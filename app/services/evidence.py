"""统一证据引用（可信度架构 batch3 / A1）。

代码内部名 EvidenceRef；界面文案用「证据引用」，勿把英文学名露出给用户。
规则 / 补盲 / 质量 / 追问 / 报告共用同一票据形状，可追溯到原文版本与坐标。
"""
from __future__ import annotations

import hashlib
import re
from typing import Any, Literal, Optional

from pydantic import BaseModel

VerificationStatus = Literal["verified", "unverified", "ambiguous", "missing"]
ParseSource = Literal["rules", "blind", "quality", "ask", "fact", "objection"]

_QUOTE_TRIM = "「」\"'“”『』…."


class EvidenceRef(BaseModel):
    """证据票据：同一发现可追到文档版本 + 坐标。

    evidence_id（宪法 P1/证据法批）：稳定标识 = document_version + quote +
    span + clause_id + parse_source 的哈希——同一发现跨层重现时 ID 一致，
    后续层引用而非重新搜索（Single Evidence Fact，第十六条）。"""

    evidence_id: str = ""
    document_version: str = ""
    quote: str = ""
    start: Optional[int] = None
    end: Optional[int] = None
    clause_id: Optional[str] = None
    verification: VerificationStatus = "unverified"
    parse_source: ParseSource = "rules"

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump()


def document_version_for(text: str, review_id: str | None = None) -> str:
    """文档版本指纹：同一审查内稳定；合同改字即变。可选挂 review_id 前缀。"""
    digest = hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]
    if review_id:
        return f"{review_id}:{digest}"
    return digest


def locate_quote_span(text: str, quote: str) -> tuple[Optional[int], Optional[int], str]:
    """在原文定位摘句起止。返回 (start, end, status) status∈verified|ambiguous|missing。"""
    bare = (quote or "").strip().strip(_QUOTE_TRIM).strip()
    if not text or not bare:
        return None, None, "missing"
    # 精确命中
    positions = _find_all(text, bare)
    if not positions:
        compact_q = re.sub(r"\s+", "", bare.replace("…", "").replace("...", ""))
        if len(compact_q) >= 6:
            positions = _find_compressed(text, compact_q)
    if not positions:
        return None, None, "missing"
    if len(positions) > 1:
        # 多命中：坐标取首处，核验标 ambiguous（位置不唯一）
        s = positions[0]
        return s, s + len(bare), "ambiguous"
    s = positions[0]
    # 压缩命中时 end 按原文长度估；精确命中用 quote 长
    end = s + len(bare)
    if end > len(text) or text[s:end] != bare:
        # 压缩路径：向后扫到等长非空白
        end = min(len(text), s + max(len(bare), 1))
    return s, end, "verified"


def build_evidence(
    *,
    text: str,
    quote: str,
    parse_source: ParseSource,
    document_version: str = "",
    clause_id: Optional[str] = None,
    start: Optional[int] = None,
    end: Optional[int] = None,
    clause_index: Optional[dict[str, Any]] = None,
    force_verification: Optional[VerificationStatus] = None,
) -> dict[str, Any]:
    """组装 EvidenceRef dict。优先用传入坐标；否则 locate；条款可从坐标派生。"""
    ver: VerificationStatus
    if force_verification:
        ver = force_verification
        s, e = start, end
    elif isinstance(start, int) and isinstance(end, int) and 0 <= start < end <= len(text or ""):
        s, e = start, end
        # 有坐标且摘句对得上 → verified；摘句空 → missing
        if not (quote or "").strip():
            ver = "missing"
            s, e = None, None
        else:
            ver = "verified"
    else:
        s, e, loc = locate_quote_span(text or "", quote or "")
        ver = loc  # type: ignore[assignment]

    cid = clause_id
    if not cid and isinstance(s, int) and clause_index:
        cid = _clause_at(clause_index, s)

    ref = EvidenceRef(
        document_version=document_version or document_version_for(text or ""),
        quote=(quote or "")[:300],
        start=s,
        end=e,
        clause_id=cid or None,
        verification=ver,
        parse_source=parse_source,
    )
    # 稳定 ID：同输入同 ID（可引用、可去重、可追溯跨层是否同一发现）。
    # 第三轮审计 P1：missing（未能定位到原文）= 无证据资格，不给 ID——
    # 「空 ID 的票据」不得通过 API 门禁冒充可引用证据。
    ref.evidence_id = (
        evidence_id_for(ref.model_dump()) if ver in ("verified", "ambiguous") else ""
    )
    return ref.to_dict()


def evidence_id_for(ref: dict[str, Any]) -> str:
    """由票据内容推导稳定 evidence_id（无需中心化发号）。

    Codex P2：parse_source 不入哈希——同一 span 被 rules/quality 两层发现
    时必须同 ID（跨层引用与去重依赖身份一致）；来源在票据字段保留溯源。
    """
    blob = "".join([
        str(ref.get("document_version") or ""),
        str(ref.get("quote") or ""),
        str(ref.get("start")),
        str(ref.get("end")),
        str(ref.get("clause_id") or ""),
    ])
    return "ev-" + hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


def normalize_evidence_ref(ref: dict[str, Any], text: str) -> dict[str, Any]:
    """票据归一化（第三轮审计 P2）：历史票据补定位、清非资格 ID、重算合格 ID。

    - verified/ambiguous 但缺坐标：用 text 重新定位（与新生成票据同锚定，
      否则旧表示与新表示哈希不同，跨层去重失效）；
    - missing/unverified（含历史误发的 ev-*）：清空 evidence_id——
      无证据资格的票据不得被引用（STORE_TTL=0 时否则永续暴露）；
    - 合格票据：按最终内容重算 ID。
    """
    ref = dict(ref)
    if ref.get("verification") in ("verified", "ambiguous") and (
        not isinstance(ref.get("start"), int) or not isinstance(ref.get("end"), int)
    ):
        s2, e2, loc = locate_quote_span(text or "", ref.get("quote") or "")
        if loc in ("verified", "ambiguous"):
            ref["start"], ref["end"], ref["verification"] = s2, e2, loc
        else:
            # 第三轮复核 P1-1：重新定位仍失败 = 无法证明原文存在——
            # 强制降级 missing、清坐标，绝不带着 verified 标签发 ID
            ref["verification"] = "missing"
            ref["start"] = None
            ref["end"] = None
    if ref.get("verification") in ("verified", "ambiguous"):
        ref["evidence_id"] = evidence_id_for(ref)
    else:
        ref["verification"] = "missing" if ref.get("verification") not in (
            "missing", "unverified") else ref.get("verification")
        ref["start"] = None
        ref["end"] = None
        ref["evidence_id"] = ""
    return ref


def attach_evidence_to_item(
    item: dict[str, Any],
    *,
    text: str,
    parse_source: ParseSource,
    document_version: str = "",
    clause_index: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """就地给 item 挂 evidence（不改 status）。已有 evidence 则补全缺失字段。"""
    existing = item.get("evidence")
    if isinstance(existing, dict) and existing.get("quote") is not None:
        # 已有票据（含历史记录）：统一归一化（第三轮审计 P1/P2）——
        # 补字段、重新定位、按资格重算或清除 ID
        if not existing.get("document_version") and document_version:
            existing["document_version"] = document_version
        if not existing.get("clause_id"):
            primary = item.get("primary_clause_id") or (
                (item.get("clause_ids") or [None])[0]
            )
            if primary:
                existing["clause_id"] = primary
        item["evidence"] = normalize_evidence_ref(existing, text)
        return item

    start = item.get("evidence_start")
    end = item.get("evidence_end")
    quote = item.get("quote") or ""
    primary = item.get("primary_clause_id") or (
        (item.get("clause_ids") or [None])[0]
    )
    if not quote and not (isinstance(start, int) and isinstance(end, int)):
        item["evidence"] = build_evidence(
            text=text,
            quote="",
            parse_source=parse_source,
            document_version=document_version,
            force_verification="missing",
        )
        return item

    item["evidence"] = build_evidence(
        text=text,
        quote=quote,
        parse_source=parse_source,
        document_version=document_version,
        clause_id=str(primary) if primary else None,
        start=start if isinstance(start, int) else None,
        end=end if isinstance(end, int) else None,
        clause_index=clause_index,
    )
    return item


def read_clause_label(
    clause_index: Optional[dict[str, Any]], coverage: Optional[dict[str, Any]]
) -> str:
    """阅读范围旁注：有条款数据时「已读第 x–y 段」。"""
    if not coverage or not clause_index:
        return ""
    clauses = clause_index.get("clauses") or []
    if not clauses:
        return ""
    unread = coverage.get("unread_ranges") or []
    covered_chars = int(coverage.get("chars_covered") or 0)
    original = int(coverage.get("original_chars") or 0)
    if not coverage.get("limited") and covered_chars >= original > 0:
        return ""
    # 用 unread 反推已读条款序号（1-based 展示）
    unread_set: set[int] = set()
    for rng in unread:
        if not isinstance(rng, (list, tuple)) or len(rng) < 2:
            continue
        lo, hi = int(rng[0]), int(rng[1])
        for i, c in enumerate(clauses):
            cs, ce = int(c.get("start") or 0), int(c.get("end") or 0)
            if cs < hi and ce > lo:
                unread_set.add(i)
    read_idxs = [i for i in range(len(clauses)) if i not in unread_set]
    if not read_idxs:
        # 无 unread 信息时按 chars 比例估前 N 段
        if covered_chars <= 0 or original <= 0:
            return ""
        n = max(1, int(round(len(clauses) * covered_chars / original)))
        n = min(n, len(clauses))
        return f"已读第 1–{n} 段" if n > 1 else "已读第 1 段"
    first, last = read_idxs[0] + 1, read_idxs[-1] + 1
    if first == last:
        return f"已读第 {first} 段"
    return f"已读第 {first}–{last} 段"


# ---- internals ----

def _find_all(text: str, needle: str, limit: int = 20) -> list[int]:
    positions: list[int] = []
    if not needle:
        return positions
    idx = text.find(needle)
    while idx >= 0 and len(positions) < limit:
        positions.append(idx)
        idx = text.find(needle, idx + 1)
    return positions


def _find_compressed(text: str, needle: str, limit: int = 20) -> list[int]:
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


def _clause_at(clause_index: dict[str, Any], pos: int) -> Optional[str]:
    clauses = clause_index.get("clauses") or []
    starts = [int(c.get("start") or 0) for c in clauses]
    if not starts:
        return None
    lo, hi = 0, len(starts) - 1
    result = -1
    while lo <= hi:
        mid = (lo + hi) // 2
        if starts[mid] <= pos:
            result = mid
            lo = mid + 1
        else:
            hi = mid - 1
    if result >= 0:
        return str(clauses[result].get("id") or "") or None
    return None


def normalize_verify_state(
    state: dict[str, Any], *, text: str, document_version: str,
    warnings: Optional[list[dict[str, str]]] = None, base_where: str = "verify.questions",
) -> dict[str, Any]:
    """归一化 verify.questions[].evidence 并同步问题级 verification（PR review P1）。

    - 票据级：同 normalize_evidence_ref（重新定位失败降级 missing、清坐标、清 ID）
    - 问题级 sibling verification 必须随票据同步：UI 渲染的是它（app.js
      「摘句对得上」），票据降了 missing 而问题还挂 verified = 把无法定位的
      摘句继续告诉用户「已验证」（PR review P1-b）
    - document_version 回填：旧票据空版本直接哈希 = 不同合同同摘句同 ID，
      击穿 document 维度的身份隔离（PR review P2）
    - warnings（2a）：改写前捕获归一化异常（批 2a 审计修订一——报警器
      不能先擦掉报警记录），调用方传 list 时追加，否则零开销
    """
    import copy

    out = copy.deepcopy(state)
    for i, q in enumerate(out.get("questions") or []):
        if not isinstance(q, dict):
            continue
        ev = q.get("evidence")
        if not isinstance(ev, dict):
            continue
        where = f"{base_where}[{i}].evidence"
        old_ev = dict(ev)
        if not ev.get("document_version"):
            ev["document_version"] = document_version
        if ev.get("quote") is not None:
            q["evidence"] = normalize_evidence_ref(ev, text or "")
            q["verification"] = q["evidence"]["verification"]
            if warnings is not None:
                w = _anomaly(old_ev, q["evidence"], where)
                if w:
                    warnings.append(w)
    return out


def _anomaly(
    old: dict[str, Any], new: dict[str, Any], where: str
) -> Optional[dict[str, str]]:
    """比对归一化前后的票据，产出异常记录（无异常返回 None）。

    reason 语义（批 2a 实现稿 §2）：
    - downgraded_unlocatable：原标 verified/ambiguous 但重新定位失败被降级
    - id_recomputed：合格票据的 ID 与改写前不一致（补定位/补版本/陈旧 ID）
    - unqualified_id：不合格票据曾带 ID，被清除
    """
    old_id = str(old.get("evidence_id") or "")
    new_id = str(new.get("evidence_id") or "")
    old_ver = old.get("verification")
    new_ver = new.get("verification")
    qualified = ("verified", "ambiguous")
    if old_ver in qualified and new_ver not in qualified:
        reason = "downgraded_unlocatable"
    elif new_ver in qualified and new_id != old_id:
        reason = "id_recomputed"
    elif new_ver not in qualified and old_id:
        reason = "unqualified_id"
    else:
        return None
    return {
        "where": where,
        "old_evidence_id": old_id,
        "new_evidence_id": new_id,
        "reason": reason,
    }


def normalize_review_evidence(
    row: dict[str, Any], warnings: Optional[list[dict[str, str]]] = None
) -> dict[str, Any]:
    """对整条审查记录的全部 EvidenceRef 容器做读路径归一化（第三轮复核 P1-2）。

    覆盖容器（此前只归一化 items[].evidence，其余五处漏网）：
    - items[].evidence
    - blind_candidates[].evidence
    - quality.observations[].evidence / quality.facts[].evidence
    - 顶层 facts[].evidence
    - objections.objections[].evidence
    - verify.questions[].evidence（含问题级 verification 同步，见
      normalize_verify_state）

    warnings（批 2a 审计修订一）：传 list 时，归一化**改写票据前**逐条
    捕获异常（where/old_evidence_id/new_evidence_id/reason）——归一化之后
    旧 ID 已被清掉，「报警器不能先擦掉报警记录」。不写回数据库，仅本次
    读取内可见，供 build_evidence_registry 生成 broken 账目。

    返回归一化后的深拷贝（不突变 store 行）。
    """
    import copy

    out = copy.deepcopy(row)

    def _fix(item_or_obj: Any, where: str) -> None:
        if isinstance(item_or_obj, dict):
            ev = item_or_obj.get("evidence")
            if isinstance(ev, dict) and ev.get("quote") is not None:
                # PR review P2：空版本回填行级 document_version，防跨合同撞 ID
                if not ev.get("document_version"):
                    ev["document_version"] = out.get("document_version") or ""
                item_or_obj["evidence"] = normalize_evidence_ref(ev, out.get("text") or "")
                if warnings is not None:
                    # 门禁 P3-2：改写前快照只在捕获路径付费（干净路径零开销）
                    w = _anomaly(dict(ev), item_or_obj["evidence"], where)
                    if w:
                        warnings.append(w)

    for i, it in enumerate(out.get("items") or []):
        _fix(it, f"items[{i}].evidence")
    for i, cand in enumerate(out.get("blind_candidates") or []):
        _fix(cand, f"blind_candidates[{i}].evidence")
    quality = out.get("quality") or {}
    if isinstance(quality, dict):
        for i, obs in enumerate(quality.get("observations") or []):
            _fix(obs, f"quality.observations[{i}].evidence")
        for i, f in enumerate(quality.get("facts") or []):
            _fix(f, f"quality.facts[{i}].evidence")
    for i, f in enumerate(out.get("facts") or []):
        _fix(f, f"facts[{i}].evidence")
    objections = out.get("objections") or {}
    if isinstance(objections, dict):
        for i, ob in enumerate(objections.get("objections") or []):
            _fix(ob, f"objections.objections[{i}].evidence")
    verify = out.get("verify") or {}
    if isinstance(verify, dict):
        # 问题级 verification 同步走专用函数（_fix 只管票据本身）
        out["verify"] = normalize_verify_state(
            verify,
            text=out.get("text") or "",
            document_version=out.get("document_version") or "",
            warnings=warnings,
        )
    return out


_REGISTRY_QUALIFIED = ("verified", "ambiguous")
_BROKEN_REFS_SAMPLE_CAP = 20
# 批 2a PR review P2-b：ID 形状防御（设计稿 §4.10——非 ev- 前缀或长度
# 不符的 ID 视为 broken 引用，不参与关联账目）
_ID_SHAPE = re.compile(r"ev-[0-9a-f]{12}")


def build_evidence_registry(
    row_normalized: dict[str, Any], warnings: list[dict[str, str]]
) -> dict[str, Any]:
    """证据登记簿概览（批 2a）：纯读路径派生视图，每次响应即时重建。

    可重建性的最高形态是「根本不持久化」——不落 store 行、不接写路径，
    零写失败风险、零迁移、天然不违反「归一化不突变 store 行」的保证。

    语义边界（批 2a 实现稿 §4.2，防过度承诺）：evidence_id 哈希含 quote
    本身，标点差异即不同 ID——multi_source_unique 只统计**现有 ID 的精确
    一致性**，不宣称语义级同证据合并（后者是批 2b 服务端 span 规范化）。
    """
    from datetime import datetime, timezone

    occurrence_total = 0
    qualified_occurrence_total = 0
    empty_id_occurrences = 0
    id_containers: dict[str, set[str]] = {}
    qualified_ids: set[str] = set()
    registry_broken: list[dict[str, str]] = []

    def _count(ev: Any, container: str) -> None:
        nonlocal occurrence_total, qualified_occurrence_total, empty_id_occurrences
        if not isinstance(ev, dict):
            return
        occurrence_total += 1
        eid = str(ev.get("evidence_id") or "")
        qualified = ev.get("verification") in _REGISTRY_QUALIFIED
        if qualified:
            qualified_occurrence_total += 1
        # 批 2a PR review P2-b：登记簿自检——归一化会跳过 quote 缺失的票据
        # （_fix 的门控条件），这类票据带着可疑 ID 混进来时归一化 warnings
        # 抓不到（broken_ref_count 恒 0）。登记簿自己验：不合格带 ID / ID
        # 形状非法 / 缺 quote 却挂合格标签，一律记 broken 并逐出关联账目。
        problems: list[str] = []
        if eid and not qualified:
            problems.append("unqualified_id_at_registry")
        if eid and qualified and not _ID_SHAPE.fullmatch(eid):
            problems.append("malformed_id")
        if not (ev.get("quote") or "").strip() and (eid or qualified):
            problems.append("quote_missing")
        if problems:
            registry_broken.append({
                "where": container,
                "old_evidence_id": eid,
                "new_evidence_id": "",
                "reason": problems[0],
            })
            return  # 逐出：不计 unique / multi_source / qualified_unique
        if qualified and eid:
            qualified_ids.add(eid)
        if eid:
            id_containers.setdefault(eid, set()).add(container)
        else:
            empty_id_occurrences += 1  # 空 ID 无法去重，各计一张

    items = row_normalized.get("items") or []
    for it in items:
        _count(it.get("evidence") if isinstance(it, dict) else None, "items")
    for cand in row_normalized.get("blind_candidates") or []:
        _count(cand.get("evidence") if isinstance(cand, dict) else None, "blind_candidates")
    quality = row_normalized.get("quality") or {}
    if isinstance(quality, dict):
        for obs in quality.get("observations") or []:
            _count(obs.get("evidence") if isinstance(obs, dict) else None, "quality.observations")
        for f in quality.get("facts") or []:
            _count(f.get("evidence") if isinstance(f, dict) else None, "quality.facts")
    for f in row_normalized.get("facts") or []:
        _count(f.get("evidence") if isinstance(f, dict) else None, "facts")
    objections = row_normalized.get("objections") or {}
    if isinstance(objections, dict):
        for ob in objections.get("objections") or []:
            _count(ob.get("evidence") if isinstance(ob, dict) else None, "objections.objections")
    verify = row_normalized.get("verify") or {}
    if isinstance(verify, dict):
        for q in verify.get("questions") or []:
            _count(q.get("evidence") if isinstance(q, dict) else None, "verify.questions")

    return {
        "registry_version": 1,
        "rebuilt_at": datetime.now(timezone.utc).isoformat(),
        "occurrence_total": occurrence_total,
        "unique_total": len(id_containers) + empty_id_occurrences,
        "qualified_occurrence_total": qualified_occurrence_total,
        "qualified_unique_total": len(qualified_ids),
        "multi_source_unique": sum(
            1 for cs in id_containers.values() if len(cs) >= 2
        ),
        # broken 账目两个来源（PR review P2-b 后）：①归一化改写前的捕获
        # （审计修订一——旧 ID 被清掉后只有这里能报警）②登记簿自检
        # （归一化跳过的票据/形状非法 ID，归一化 warnings 盲区）
        "broken_ref_count": len(warnings) + len(registry_broken),
        "broken_refs": list((list(warnings) + registry_broken)[
            :_BROKEN_REFS_SAMPLE_CAP]),
    }
