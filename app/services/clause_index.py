"""条款索引（阶段 1.1）：把合同全文切成带稳定编号与字符坐标的条款清单。

职责边界（见 docs/roadmap-llm-ui.md 阶段 1.1）：
- 纯确定性切分，无 LLM 参与；坐标锚定 node_parse 产物（即入库的 text 全文）。
- 双策略：numbered（「第X条」编号型，回放语料主路径）/ paragraph（流水型回退，
  按段落贪心聚合——提取层无字号信息，不做字号分桶）。
- 稳定编号用顺序号 c01…：同文本确定性切分 → 编号天然稳定；内容哈希会在
  文本一字之改时全表漂移且不可读，否决。
- map_items_to_clauses 是纯展示增强：整段 try/except，任何异常返回空映射，
  绝不让审查失败；只给 item 追加 clause_ids 字段，既有字段一字不动
  （item.id 是前端→/api/ask→store 三方 join key 红线）。
"""
from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# 编号条款标题：行首「第X条」（中文数字含两/〇/零 + 阿拉伯数字），后接可选分隔符。
# 行首锚定依赖提取层保留换行（docling/python-docx/pypdf 三条路径均满足）。
_HEADING_LINE_RE = re.compile(
    r"(?m)^[ \t]*(第[零〇一二三四五六七八九十百千两0-9]+条)[ \t]*[、.．:：]?[ \t]*(.*)$"
)

# paragraph 回退聚合参数：目标桶 800 字、硬上限 1200（超限单段硬切）
_BUCKET_TARGET = 800
_BUCKET_MAX = 1200

# numbered 策略生效条件：≥2 个编号条款；否则视为流水型文本走 paragraph 回退。
# 「第一条」之前的 preamble（当事人信息/鉴于条款）作为首个条款建档，不设
# 覆盖率阈值——连续文本里最后一个编号条款总会计入其后全部正文，覆盖率
# 规则构造不出可靠的降级信号（实测），preamble 建档比阈值更诚实。
_MIN_CLAUSES = 2
_MIN_PREFIX = 20

_WHITESPACE_RE = re.compile(r"\s+")


def build_clause_index(text: str) -> dict[str, Any]:
    """构建条款索引。返回 {"strategy", "count", "clauses":[{id,heading,start,end,chars}]}。

    start/end 为闭开区间 [start, end) 字符偏移，锚定入参 text 本身。
    公开形状不含条款正文（审查记录里全文已有，索引只存元数据）；
    paragraph 桶定位失败（理论上仅剩空白噪声）时丢弃该桶。
    """
    text = text or ""
    if not text.strip():
        return {"strategy": "paragraph", "count": 0, "clauses": []}

    clauses = _split_numbered(text)
    strategy = "numbered"
    if len(clauses) < _MIN_CLAUSES:
        strategy = "paragraph"
        clauses = _split_paragraphs(text)
        clauses = _resolve_offsets(text, clauses)
    return _finalize(strategy, clauses)


def _finalize(strategy: str, clauses: list[dict[str, Any]]) -> dict[str, Any]:
    for i, clause in enumerate(clauses, start=1):
        clause["id"] = f"c{i:02d}"
        clause["chars"] = clause["end"] - clause["start"]
    return {"strategy": strategy, "count": len(clauses), "clauses": clauses}


def _split_numbered(text: str) -> list[dict[str, Any]]:
    matches = list(_HEADING_LINE_RE.finditer(text))
    clauses: list[dict[str, Any]] = []

    # 「第一条」之前的 preamble（标题页/当事人信息/鉴于条款）建档为首个条款
    first_start = matches[0].start() if matches else len(text)
    prefix = text[:first_start].strip()
    if len(prefix) >= _MIN_PREFIX:
        clauses.append(
            {"heading": prefix[:20] + ("…" if len(prefix) > 20 else ""), "start": 0, "end": first_start}
        )

    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        # 空条款（标题行之后直到下一标题零内容）跳过不建档
        if not text[m.end() : end].strip():
            continue
        clauses.append({"heading": m.group(0).strip(), "start": m.start(), "end": end})
    return clauses


def _split_paragraphs(text: str) -> list[dict[str, Any]]:
    """流水型回退：按段落贪心聚合成 ≤1200 字的伪条款桶。"""
    paragraphs = [p for p in (seg.strip() for seg in text.split("\n")) if p]
    if not paragraphs:
        # 纯空白/无换行长文本整体成桶（硬切超长）
        return _hard_split(text)

    buckets: list[dict[str, Any]] = []
    current: list[str] = []
    current_len = 0
    for para in paragraphs:
        # 单段本身超上限：先冲刷当前桶，再对超长段硬切
        if len(para) > _BUCKET_MAX:
            if current:
                buckets.append(_make_bucket(current))
                current, current_len = [], 0
            buckets.extend(_hard_split(para))
            continue
        if current and current_len + len(para) > _BUCKET_TARGET:
            buckets.append(_make_bucket(current))
            current, current_len = [], 0
        current.append(para)
        current_len += len(para)
    if current:
        buckets.append(_make_bucket(current))
    return buckets


def _make_bucket(paragraphs: list[str]) -> dict[str, Any]:
    heading = paragraphs[0][:20] + ("…" if len(paragraphs[0]) > 20 else "")
    # 锚点取首段前 50 字 / 末段后 50 字（strip 只去首尾空白 → 必为原文逐字子串；
    # 桶体 join 不是原文子串——原文分隔符可能是 \r\n 或连续换行，不能拿来 find）
    return {
        "heading": heading,
        "start": -1,
        "end": -1,
        "anchor_head": paragraphs[0][:50],
        "anchor_tail": paragraphs[-1][-50:],
    }


def _hard_split(text: str) -> list[dict[str, Any]]:
    stripped = text.strip()
    if not stripped:
        return []
    return [
        {
            "heading": stripped[i : i + 20] + ("…" if len(stripped) - i > 20 else ""),
            "start": -1,
            "end": -1,
            "anchor_head": stripped[i : i + 50],
            "anchor_tail": stripped[i : min(len(stripped), i + _BUCKET_MAX)][-50:],
        }
        for i in range(0, len(stripped), _BUCKET_MAX)
    ]


def _resolve_offsets(text: str, clauses: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把 paragraph 策略产出的桶（start=-1）定位回全文坐标。

    头锚点定起点、尾锚点定终点（从起点向后找）；任一锚点定位失败则丢弃
    该桶（理论上仅剩空白噪声场景）。
    """
    out: list[dict[str, Any]] = []
    search_from = 0
    for clause in clauses:
        if clause.get("start", -1) >= 0:
            out.append(clause)
            continue
        head = clause.get("anchor_head") or ""
        tail = clause.get("anchor_tail") or head
        idx = text.find(head, search_from) if head else -1
        if idx < 0:
            continue
        tail_idx = text.find(tail, idx)
        end = tail_idx + len(tail) if tail_idx >= 0 else idx + len(head)
        out.append(
            {k: v for k, v in clause.items() if k not in ("anchor_head", "anchor_tail")}
            | {"start": idx, "end": min(end, len(text))}
        )
        search_from = idx
    return out


def map_items_to_clauses(
    items: list[dict[str, Any]], clause_index: dict[str, Any], text: str
) -> None:
    """给每个 item 就地附加 clause_ids（命中所在的条款 id 列表，升序去重）。

    锚点优先用 hits（规则命中的全文逐字子串，比装饰过的 quote 可靠——
    quote 有 … 装饰且换行被替换为空格）；hits 定位不到时用 quote 去装饰
    后的空白压缩匹配回退。映射是纯展示增强：任何异常静默降级为空映射。
    """
    try:
        clauses = clause_index.get("clauses") or []
        starts = [c["start"] for c in clauses]
        for item in items:
            item["clause_ids"] = _locate_item(item, clauses, starts, text)
    except Exception:  # noqa: BLE001 — 展示增强不允许影响审查主流程
        logger.warning("clause mapping failed; falling back to empty mapping", exc_info=True)
        for item in items or []:
            try:
                item["clause_ids"] = []
            except Exception:  # noqa: BLE001
                pass


def _locate_item(
    item: dict[str, Any], clauses: list[dict[str, Any]], starts: list[int], text: str
) -> list[str]:
    positions: list[int] = []
    for hit in item.get("hits") or []:
        positions.extend(_find_all(text, hit))
    if not positions:
        quote = _normalized_quote(item.get("quote") or "")
        if quote:
            positions.extend(_find_compressed(text, quote))
    if not positions:
        return []

    found = set()
    for pos in positions:
        idx = _bucket_for(pos, starts)
        if 0 <= idx < len(clauses):
            found.add(clauses[idx].get("id", ""))
    return sorted(cid for cid in found if cid)


def _bucket_for(pos: int, starts: list[int]) -> int:
    """pos 落入哪个 [start, next_start) 区间；-1 坐标（未定位桶）不参与。"""
    lo, hi = 0, len(starts) - 1
    result = -1
    while lo <= hi:
        mid = (lo + hi) // 2
        if starts[mid] <= pos:
            result = mid
            lo = mid + 1
        else:
            hi = mid - 1
    return result if result >= 0 and starts[result] >= 0 else -1


def _find_all(text: str, needle: str, limit: int = 20) -> list[int]:
    positions: list[int] = []
    if not needle:
        return positions
    idx = text.find(needle)
    while idx >= 0 and len(positions) < limit:
        positions.append(idx)
        idx = text.find(needle, idx + 1)
    return positions


def _normalized_quote(quote: str) -> str:
    """quote 去装饰：剥掉省略号并压缩全部空白（对齐 _quote_supported 容差思路）。"""
    return _WHITESPACE_RE.sub("", quote.replace("…", ""))


def _find_compressed(text: str, needle: str, limit: int = 20) -> list[int]:
    """空白压缩域内查找，返回命中的原文坐标。

    构建「压缩后字符 → 原文偏移」映射表，在压缩串里 find 后映射回原文位置。
    """
    if not needle:
        return []
    compressed_chars: list[str] = []
    offsets: list[int] = []
    for i, ch in enumerate(text):
        if not ch.isspace():
            compressed_chars.append(ch)
            offsets.append(i)
    compressed = "".join(compressed_chars)
    positions: list[int] = []
    idx = compressed.find(needle)
    while idx >= 0 and len(positions) < limit:
        positions.append(offsets[idx])
        idx = compressed.find(needle, idx + 1)
    return positions
