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

# 编号条款标题：行首「第X条」（中文数字含两/〇/零 + 半/全角阿拉伯数字），
# 允许半/全角空格缩进（中文 Word 合同全角空格排版常见，门禁 P2-1），
# 后接可选分隔符。行首锚定依赖提取层保留换行（三条提取路径均满足）。
_HEADING_LINE_RE = re.compile(
    r"(?m)^[ \t　]*(第[零〇一二三四五六七八九十百千两0-9０-９]+条)[ \t　]*[、.．:：]?[ \t　]*(.*)$"
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
    公开形状不含条款正文（审查记录里全文已有，索引只存元数据）。
    两种策略的坐标都来自切分本身（finditer 命中点/段落偏移），不做
    事后锚点回捞——重复内容文本的回捞会塌缩（门禁 P1-1 教训）。
    """
    text = text or ""
    if not text.strip():
        return {"strategy": "paragraph", "count": 0, "clauses": []}

    clauses = _split_numbered(text)
    strategy = "numbered"
    if len(clauses) < _MIN_CLAUSES:
        strategy = "paragraph"
        clauses = _split_paragraphs(text)
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
        # 空条款跳过不建档。注意标题正则的 (.*)$ 会把同行内容一并捕获——
        # 「第五条 违约责任：甲方…」这类单行条款在 m.end() 之后零内容但
        # group(2) 有标题文字，不算空（审计二轮 P1 实证：否则整条被吞、
        # 证据坐标落进上一条款）。标题 token 后无文字且后随零内容才算空。
        title_tail = (m.group(2) or "").strip()
        if not title_tail and not text[m.end() : end].strip():
            continue
        clauses.append({"heading": m.group(0).strip(), "start": m.start(), "end": end})
    return clauses


def _split_paragraphs(text: str) -> list[dict[str, Any]]:
    """流水型回退：按段落贪心聚合（目标 800 字、单段超 1200 硬切）。

    坐标直接来自切分本身（原文偏移），**不做事后锚点回捞**——重复内容
    文本（模板句/PDF 提取退化）的锚点 find 会塌缩到同一坐标，导致分段
    阅读静默漏读绝大部分正文（门禁 P1-1 实测教训）。
    """
    paras: list[tuple[int, int, str]] = []  # (start, end, stripped) 原文坐标
    pos = 0
    for seg in text.split("\n"):
        stripped = seg.strip()
        if stripped:
            start = pos + (len(seg) - len(seg.lstrip()))
            paras.append((start, start + len(stripped), stripped))
        pos += len(seg) + 1
    if not paras:
        return []

    buckets: list[dict[str, Any]] = []
    current: list[tuple[int, int, str]] = []
    current_len = 0

    def flush() -> None:
        nonlocal current, current_len
        if current:
            first_text = current[0][2]
            buckets.append(
                {
                    "heading": first_text[:20] + ("…" if len(first_text) > 20 else ""),
                    "start": current[0][0],
                    "end": current[-1][1],
                }
            )
            current, current_len = [], 0

    for start, end, stripped in paras:
        size = end - start
        if size > _BUCKET_MAX:
            # 单段本身超上限：冲刷当前桶后按字符硬切（切分内坐标，无回捞）
            flush()
            for i in range(start, end, _BUCKET_MAX):
                hi = min(end, i + _BUCKET_MAX)
                head = text[i:hi]
                buckets.append(
                    {"heading": head[:20] + ("…" if hi - i > 20 else ""), "start": i, "end": hi}
                )
            continue
        if current and current_len + size > _BUCKET_TARGET:
            flush()
        current.append((start, end, stripped))
        current_len += size
    flush()
    return buckets


def build_clause_context(
    text: str,
    clause_index: dict[str, Any],
    clause_ids: list[str],
    max_chars: int = 4000,
    neighbors: int = 1,
    primary_clause_id: str = "",
) -> str:
    """按命中条款构造追问上下文（外部审计批1-③；二轮 P2 升级装填顺序）。

    装填顺序：**primary 条款（证据所在）→ primary 邻居 → 其他相关条款**，
    不再按文档顺序——同词（如「违约」）在数十个条款出现时，真风险条款
    （证据坐标所在）可能排在很后，按顺序装填会被前面的挤出预算。
    总量截到 max_chars（延迟护栏）。任何异常/未定位返回空串，调用方回退
    头尾采样——本函数是纯增强，绝不成为追问失败原因。
    """
    try:
        clauses = clause_index.get("clauses") or []
        if not clauses or not text:
            return ""
        id_set = {cid for cid in clause_ids if isinstance(cid, str)}
        if primary_clause_id:
            id_set.add(primary_clause_id)
        if not id_set:
            return ""
        id_to_idx = {str(c.get("id")): i for i, c in enumerate(clauses)}

        wanted: list[int] = []

        def _add_with_neighbors(i: int) -> None:
            # primary 居首，邻居按距离（前/后）随后——保证块内 primary 最先装填
            order = [i]
            for d in range(1, neighbors + 1):
                if i - d >= 0:
                    order.append(i - d)
                if i + d < len(clauses):
                    order.append(i + d)
            for j in order:
                if j not in wanted:
                    wanted.append(j)

        # 1) primary 条款与其邻居绝对优先
        primary_idx = id_to_idx.get(primary_clause_id, -1) if primary_clause_id else -1
        if primary_idx >= 0:
            _add_with_neighbors(primary_idx)
        # 2) 其他相关条款按序补入
        for i, c in enumerate(clauses):
            if c.get("id") in id_set and i not in wanted:
                _add_with_neighbors(i)

        parts: list[str] = []
        total = 0
        # 按插入序装填（primary 块在最前）——sorted 会把优先级打回文档序
        for j in wanted:
            c = clauses[j]
            start, end = c.get("start", -1), c.get("end", -1)
            if not isinstance(start, int) or not isinstance(end, int):
                continue
            if not (0 <= start < end <= len(text)):
                continue
            body = text[start:end]
            if total + len(body) > max_chars:
                body = body[: max_chars - total]
            parts.append(body)
            total += len(body)
            if total >= max_chars:
                break
        # join 后再截一次：分隔符不计入预算会超出 max_chars（小智娘门禁 P3）
        return "\n".join(parts)[:max_chars]
    except Exception:  # noqa: BLE001 — 纯增强，绝不抛
        return ""


def map_items_to_clauses(
    items: list[dict[str, Any]], clause_index: dict[str, Any], text: str
) -> None:
    """给每个 item 就地附加条款归属（外部审计二轮 P1-1 升级）：

    - primary_clause_id：MatchEvidence 坐标（evidence_start）所在条款——
      真正触发风险的条款，Ask 上下文的第一顺位；
    - clause_ids：hits 全部出现位置映射的相关条款（primary 置首）——
      同词多条款时的 related 语义。

    锚点优先用 hits（规则命中的全文逐字子串）；evidence 坐标由规则引擎
    一次扫描产出，不再二次 find 猜测。映射是纯展示增强：任何异常静默
    降级为空映射。
    """
    try:
        clauses = clause_index.get("clauses") or []
        starts = [c["start"] for c in clauses]
        for item in items:
            related = _locate_item(item, clauses, starts, text)
            primary = ""
            ev_start = item.get("evidence_start")
            if isinstance(ev_start, int) and ev_start >= 0:
                idx = _bucket_for(ev_start, starts)
                if 0 <= idx < len(clauses):
                    primary = str(clauses[idx].get("id") or "")
            item["clause_ids"] = ([primary] if primary else []) + [
                c for c in related if c != primary
            ]
            item["primary_clause_id"] = primary
    except Exception:  # noqa: BLE001 — 展示增强不允许影响审查主流程
        logger.warning("clause mapping failed; falling back to empty mapping", exc_info=True)
        for item in items or []:
            try:
                item["clause_ids"] = []
                item["primary_clause_id"] = ""
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
