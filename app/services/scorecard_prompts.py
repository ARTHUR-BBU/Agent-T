"""评分/分段阅读的提示词与切块（外部审计二轮 PR-E 分层）。

本模块只含「问什么、怎么切块」的确定性构造，零 scrub/计分依赖；
确定性计分（postprocess/禁语/封顶）留在 scorecard.py。既有调用方
继续从 scorecard import（re-export 兼容）。
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional

from app.prompts.guards import with_untrusted_guard
from app.prompts.versioning import source_version

# 单次文本上限（2026-09-07 线上事故常量：12000 字 + 评分指令让 glm-5.2 超 240s；
# 压到 6000 后回到 ~30-60s。头+尾采样——主体在头部、签署区在尾部）
MAX_CONTRACT_CHARS = 6000
_TAIL_CHARS = 1000
_CLIP_MARKER = "\n…(中段截断)…\n"

_CONSISTENCY_CLAUSES = """【规则结果优先条款】本评分卡是规则引擎打标结果的解释与汇总，不是独立的第二次审查。你必须遵守：
1. 规则已标「需关注」「未找到」「通过」的，你的评语与分数必须与该档位方向一致。你不得降级、弱化、推翻或"补充解释掉"任何规则档位；确有不同看法，只能以"此外提示"方式附加，且不得降低该项所在段的扣分。
2. 凡存在任一「需关注」或「未找到」项的段，该段不得给满分，评语必须如实点名该项。
3. 不得对规则档位做对冲表述（如"虽有提示但问题不大"）。认为风险轻微，表述上限是"该项属可补正的形式瑕疵，修订成本低"，扣分仍会由系统强制执行。
4. 总分、分段分与评语三者必须自洽：列举了 N 个问题就体现 N 次扣分；不允许"列举五个问题、总分92"。
5. 冲突时一律以规则档位为准，并按规则档位方向措辞。
6. 禁止出现整体性背书（如"本合同没有问题""可以放心签署"）、效力越权判断（如"该条款无效""必定败诉"）、推翻规则档位的表述（如"提示可以忽略"）、替代性声明（如"可替代律师审查"）、保证性预测（如"不会违约"）。"""


def build_system_prompt(segments: list[dict[str, Any]], policies: list[str]) -> str:
    seg_lines = []
    for s in segments:
        na_note = "（本类不适用，给 0 分并注明）" if s.get("na") else f"（满分 {s['weight']} 分）"
        seg_lines.append(f"- {s['key']}：{s['name']} {na_note}")
    seg_block = "\n".join(seg_lines)
    policy_block = "\n".join(f"- {p}" for p in policies) or "- （无额外政策）"
    prompt = f"""你是合同审查「评分卡」助手。规则引擎已对合同逐项打标（通过/需关注/未找到/本类不适用）；你的任务是**解释与汇总**规则结果，给出百分制评分卡。

{_CONSISTENCY_CLAUSES}

评分段（key：名称 满分）：
{seg_block}

政策参考（为什么这样标）：
{policy_block}

输出要求：只输出 JSON 对象（不要 markdown 围栏），结构严格为：
{{
  "scorecard": {{
    "summary": "一句话总评（≤60字，说人话，不盖章）",
    "segments": [
      {{"key":"A","score":整数,"comment":"该段一句话评语，须点名该段内的需关注/未找到项","gap_item_ids":["该段内表述弱/有缺口条目的id，没有则[]"]}}
    ]
  }},
  "candidates": [
    {{"item_id":"...","name":"...","note":"...","quote":"..."}}
  ]
}}

segments[].gap_item_ids 说明：该段内你认为「表述弱、有缺口、值得补盲」的条目 id（含规则已标「通过」但表述单薄的项），没有则输出 []。系统会把点名条目纳入定向补盲，供人工确认——这只是点名，不改规则档位。
candidates 说明：对「规则未标需关注、但你发现真实风险且能引用原文」的条目提出候选；quote 必须是合同原文连续摘录，没有原文依据就不要输出该项；无候选输出 []。禁止改写规则已有结论。"""
    return with_untrusted_guard(prompt)



PROMPT_VERSION = source_version(build_system_prompt)

def _clip_for_scoring(text: str) -> str:
    """头 + 尾采样截断（总预算 MAX_CONTRACT_CHARS）：
    主体在头部、签署/落款在尾部，中段条款密集但信噪比低。"""
    if len(text) <= MAX_CONTRACT_CHARS:
        return text
    head = MAX_CONTRACT_CHARS - _TAIL_CHARS - len(_CLIP_MARKER)
    return text[:head] + _CLIP_MARKER + text[-_TAIL_CHARS:]


def clip_contract_text(text: str) -> str:
    """公开封装：发给大模型的合同文本统一走头尾采样（评分卡与追问共用）。"""
    return _clip_for_scoring(text)


def _rule_block(items: list[dict[str, Any]]) -> str:
    lines = []
    for it in items:
        na = "（本类不适用）" if it.get("category_na") else ""
        lines.append(
            f"- {it.get('name')}（id={it.get('id')}）：{it.get('status')}{na}"
            f"｜备注：{it.get('note') or '无'}"
        )
    return "\n".join(lines)


def build_user_prompt(text: str, items: list[dict[str, Any]]) -> str:
    body = _clip_for_scoring(text)
    rule_block = _rule_block(items)
    return f"""规则引擎打标结果：
{rule_block}

合同全文：
{body}

请按系统指令只输出 JSON。"""


# ---------- 阶段 1.2 分段阅读（map-reduce）：长合同消灭 6000 字近视 ----------

def build_map_system_prompt(policies: list[str]) -> str:
    policy_block = "\n".join(f"- {p}" for p in policies) or "- （无额外政策）"
    prompt = f"""你是合同审查「分段阅读」助手。长合同被切成若干片段分批阅读，你只看到一个片段。任务：通读本片段，找出与规则清单相关的真实风险信号，产出**观察素材**。你不下结论、不打分、不改任何档位——汇总由另一轮完成。

政策参考（为什么这样找）：
{policy_block}

输出要求：只输出 JSON 对象（不要 markdown 围栏），结构严格为：
{{
  "observations": [
    {{"segment":"A","comment":"本片段内发现的具体风险点，须点明条款位置（如「第五条」）与关键表述","gap_item_ids":["表述弱/有缺口/值得补盲的清单项id"],"candidates":[{{"item_id":"...","name":"...","note":"...","quote":"合同原文连续摘录"}}]}}
  ]
}}

说明：
- segment 填该风险点所属的评分段 key；与清单无关的片段输出 {{"observations":[]}}。
- quote 必须是**本片段原文连续摘录**，没有原文依据就不要输出候选。
- 禁止整体性背书（如"没有问题""可以放心签署"）、禁止效力越权判断（如"该条款无效"）、禁止推翻规则档位的表述（如"提示可以忽略"）。"""
    return with_untrusted_guard(prompt)


def build_map_user_prompt(chunk_text: str, items: list[dict[str, Any]], part_no: int, part_total: int) -> str:
    return f"""规则引擎打标清单：
{_rule_block(items)}

合同片段（第 {part_no}/{part_total} 部分）：
{chunk_text}

请按系统指令只输出 JSON。"""


def build_reduce_user_prompt(items: list[dict[str, Any]], observations_block: str) -> str:
    """汇总轮 user prompt：规则打标 + 分段观察素材，**不含合同全文**（延迟护栏：
    输入规模与单次调用路径同量级）。"""
    return f"""规则引擎打标结果：
{_rule_block(items)}

分段阅读观察（分片阅读产生的素材，仅供定位与展开；档位与扣分一律以规则打标为准，不得因素材改判）：
{observations_block}

请按系统指令只输出 JSON（输出结构与单文本版完全一致）。"""


def parse_map_payload(raw: str) -> Optional[list[dict[str, Any]]]:
    """解析 map 轮输出。成功返回 observations 列表（合法空 = []）；解析失败返回 None
    ——调用方据此区分「无风险」与「结构错误」，二者不能混为回退依据。"""
    if not raw:
        return None
    text = raw.strip()
    fence = re.match(r"^```(?:json)?\s*([\s\S]*?)\s*```$", text)
    if fence:
        text = fence.group(1).strip()
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{[\s\S]*\}", text)
        if not m:
            return None
        try:
            obj = json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
    obs = obj.get("observations") if isinstance(obj, dict) else None
    if not isinstance(obs, list):
        return None
    return [o for o in obs if isinstance(o, dict)]



def build_review_chunks(
    text: str, clause_index: Optional[dict[str, Any]] = None, max_segments: int = 4
) -> list[str]:
    """长合同 → ≤max_segments 个阅读块（每块 ≤MAX_CONTRACT_CHARS，延迟护栏）。

    优先按条款索引对齐切块（条款不跨块）；无索引/坐标失效时按段落聚合回退。
    块数超限时尾部并成一块并走头尾采样截断——覆盖 4 块 ≈2.4 万字，超出部分
    是深度换延迟的既有取舍，显式截断好过静默假装读过。
    """
    chunks, _meta = build_review_plan(text, clause_index, max_segments)
    return chunks


def build_review_plan(
    text: str, clause_index: Optional[dict[str, Any]] = None, max_segments: int = 4
) -> tuple[list[str], dict[str, Any]]:
    """切块 + 原文覆盖元数据（可信度 P1：不得用截断后块数假装读完全文）。

    meta 字段：
      original_chars / chars_covered / unread_ranges / truncation_reason
      chunks_planned / fully_covered
    truncation_reason: None | "max_segments_clip"
    """
    text = text or ""
    empty_meta: dict[str, Any] = {
        "original_chars": 0,
        "chars_covered": 0,
        "unread_ranges": [],
        "truncation_reason": None,
        "chunks_planned": 0,
        "fully_covered": True,
    }
    if not text:
        return [], empty_meta

    ranges = _reading_ranges(text, clause_index)
    # 与历史切块同构：先产出 (chunk_text, covering_spans)
    pieces: list[tuple[str, list[tuple[int, int]]]] = []
    cur_start: Optional[int] = None
    cur_end = 0
    for start, end in ranges:
        size = end - start
        if size > MAX_CONTRACT_CHARS:
            if cur_start is not None:
                pieces.append((text[cur_start:cur_end], [(cur_start, cur_end)]))
                cur_start = None
            for i in range(start, end, MAX_CONTRACT_CHARS):
                j = min(end, i + MAX_CONTRACT_CHARS)
                pieces.append((text[i:j], [(i, j)]))
            continue
        if cur_start is None:
            cur_start, cur_end = start, end
            continue
        if cur_end - cur_start + size > MAX_CONTRACT_CHARS:
            pieces.append((text[cur_start:cur_end], [(cur_start, cur_end)]))
            cur_start, cur_end = start, end
        else:
            cur_end = end
    if cur_start is not None:
        pieces.append((text[cur_start:cur_end], [(cur_start, cur_end)]))

    # 碎块并入前块（字符串拼接，覆盖区间并入）
    merged: list[tuple[str, list[tuple[int, int]]]] = []
    for chunk, spans in pieces:
        if merged and len(chunk) < 32:
            prev_t, prev_s = merged[-1]
            merged[-1] = (prev_t + chunk, prev_s + spans)
        else:
            merged.append((chunk, list(spans)))

    truncation_reason: Optional[str] = None
    unread_ranges: list[list[int]] = []
    covered_spans: list[tuple[int, int]] = []
    chunks: list[str] = []

    if len(merged) > max_segments:
        kept = merged[: max_segments - 1]
        tail = merged[max_segments - 1 :]
        for chunk, spans in kept:
            chunks.append(chunk)
            covered_spans.extend(spans)
        tail_text = "".join(c for c, _ in tail)
        tail_spans = [sp for _, sps in tail for sp in sps]
        if len(tail_text) > MAX_CONTRACT_CHARS:
            truncation_reason = "max_segments_clip"
            head = MAX_CONTRACT_CHARS - _TAIL_CHARS - len(_CLIP_MARKER)
            chunks.append(_clip_for_scoring(tail_text))
            # 按拼接串偏移，把头/尾采样映射回各原始 span
            offset = 0
            tail_len = len(tail_text)
            tail_from = tail_len - _TAIL_CHARS
            for s, e in tail_spans:
                slen = e - s
                local_covered: list[tuple[int, int]] = []
                if offset < head:
                    lo, hi = offset, min(offset + slen, head)
                    if lo < hi:
                        local_covered.append((lo, hi))
                if offset + slen > tail_from:
                    lo, hi = max(offset, tail_from), offset + slen
                    if lo < hi:
                        local_covered.append((lo, hi))
                for lo, hi in local_covered:
                    covered_spans.append((s + (lo - offset), s + (hi - offset)))
                covered_local = _merge_spans(
                    [(lo - offset, hi - offset) for lo, hi in local_covered]
                )
                for ulo, uhi in _subtract_span((0, slen), covered_local):
                    unread_ranges.append([s + ulo, s + uhi])
                offset += slen
        else:
            chunks.append(tail_text)
            covered_spans.extend(tail_spans)
    else:
        for chunk, spans in merged:
            chunks.append(chunk)
            covered_spans.extend(spans)

    covered_spans = _merge_spans(covered_spans)
    chars_covered = sum(e - s for s, e in covered_spans)
    # 全文相对已覆盖的空洞（含条款索引缝隙 + 截断中段）
    unread_ranges = _merge_range_lists(
        unread_ranges + [[s, e] for s, e in _subtract_span((0, len(text)), covered_spans)]
    )
    fully_covered = chars_covered >= len(text) and truncation_reason is None
    meta = {
        "original_chars": len(text),
        "chars_covered": chars_covered,
        "unread_ranges": unread_ranges,
        "truncation_reason": truncation_reason,
        "chunks_planned": len(chunks),
        "fully_covered": fully_covered,
    }
    return chunks, meta


def _merge_spans(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    if not spans:
        return []
    ordered = sorted((s, e) for s, e in spans if s < e)
    out: list[tuple[int, int]] = [ordered[0]]
    for s, e in ordered[1:]:
        ps, pe = out[-1]
        if s <= pe:
            out[-1] = (ps, max(pe, e))
        else:
            out.append((s, e))
    return out


def _merge_range_lists(ranges: list[list[int]]) -> list[list[int]]:
    merged = _merge_spans([(r[0], r[1]) for r in ranges if len(r) == 2 and r[0] < r[1]])
    return [[s, e] for s, e in merged]


def _subtract_span(
    universe: tuple[int, int], covered: list[tuple[int, int]]
) -> list[tuple[int, int]]:
    """universe 减去 covered，返回剩余区间。"""
    u0, u1 = universe
    if u0 >= u1:
        return []
    gaps: list[tuple[int, int]] = []
    cursor = u0
    for s, e in _merge_spans(covered):
        if e <= cursor:
            continue
        if s > cursor:
            gaps.append((cursor, min(s, u1)))
        cursor = max(cursor, e)
        if cursor >= u1:
            break
    if cursor < u1:
        gaps.append((cursor, u1))
    return [(s, e) for s, e in gaps if s < e]


def coverage_from_plan(
    plan_meta: dict[str, Any],
    *,
    chunks_reviewed: int,
    reason: Optional[str] = None,
) -> dict[str, Any]:
    """把切块计划元数据与实际审阅块数合成对外 coverage。

    limited 永不为 false，除非原文被计划完全覆盖且计划块全部审阅成功、无额外降级原因。
    """
    chunks_total = int(plan_meta.get("chunks_planned") or 0)
    fully = bool(plan_meta.get("fully_covered"))
    trunc = plan_meta.get("truncation_reason")
    reviewed = max(0, min(int(chunks_reviewed), chunks_total)) if chunks_total else 0
    planned_chars = int(plan_meta.get("chars_covered") or 0)
    if chunks_total > 0 and reviewed < chunks_total:
        planned_chars = int(planned_chars * reviewed / chunks_total)
    effective_reason = reason or trunc
    if reviewed < chunks_total and not effective_reason:
        effective_reason = "partial_map_fail" if reviewed > 0 else "map_fail"
    if fully and reviewed >= chunks_total and not trunc and not reason:
        limited = False
        effective_reason = None
        planned_chars = int(plan_meta.get("chars_covered") or 0)
        unread: list[list[int]] = []
    else:
        limited = True
        unread = list(plan_meta.get("unread_ranges") or [])
    return {
        "chunks_total": chunks_total,
        "chunks_reviewed": reviewed,
        "limited": limited,
        "original_chars": int(plan_meta.get("original_chars") or 0),
        "chars_covered": planned_chars,
        "unread_ranges": unread,
        "truncation_reason": effective_reason,
    }


def _reading_ranges(text: str, clause_index: Optional[dict[str, Any]]) -> list[tuple[int, int]]:
    """阅读区间列表：条款对齐（索引可信）或段落聚合（回退）。区间有序且覆盖全文。

    索引可信 = 有序、互不重叠、并集覆盖 ≥90% 全文——重复内容文本曾把回退
    桶坐标塌缩到开头（覆盖 3%），若无此校验分段阅读会静默漏读还照常出分
    （门禁 P1-1）。校验不过一律走段落聚合（坐标来自切分，实测可靠）。
    """
    clauses = (clause_index or {}).get("clauses") or []
    ranges = [
        (int(c["start"]), int(c["end"]))
        for c in clauses
        if isinstance(c.get("start"), int) and isinstance(c.get("end"), int)
        and 0 <= c["start"] < c["end"] <= len(text)
    ]
    ranges.sort()
    covered = sum(end - start for start, end in ranges)
    non_overlapping = all(ranges[i][1] <= ranges[i + 1][0] for i in range(len(ranges) - 1))
    if ranges and non_overlapping and text and covered / len(text) >= 0.9:
        return ranges

    # 回退：按段落聚合到 MAX_CONTRACT_CHARS
    out: list[tuple[int, int]] = []
    pos = 0
    seg_start = 0
    for para in text.split("\n"):
        plen = len(para) + 1
        if pos - seg_start + plen > MAX_CONTRACT_CHARS and pos > seg_start:
            out.append((seg_start, pos))
            seg_start = pos
        pos += plen
    if seg_start < len(text):
        out.append((seg_start, len(text)))
    return out


