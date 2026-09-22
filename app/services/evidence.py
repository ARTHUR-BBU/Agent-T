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

# 2b-①：补中文句读——摘句尾部的句号/逗号等是引用装饰，剥离后同位
# 置的措辞变体（带句号/不带）才能收敛到同一 span（不然 4-15 与 4-16 两张票）
_QUOTE_TRIM = "「」\"'“”『』…。，、；！？"


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
    """在原文定位摘句起止。返回 (start, end, status) status∈verified|ambiguous|missing。

    批 2b-①（终审钉 4）：端点必须是原文**真实坐标**——精确命中 end =
    start+len(bare) 本就精确；压缩命中 end 由压缩文本的 offset 表回推
    （原实现按摘句长度估算， overrun/underrun 都可能）。硬测试契约：
    text[start:end] 去空白后 == 规范化摘句。
    """
    bare = (quote or "").strip().strip(_QUOTE_TRIM).strip()
    if not text or not bare:
        return None, None, "missing"
    # 精确命中
    positions = _find_all(text, bare)
    if not positions:
        compact_q = re.sub(r"\s+", "", bare.replace("…", "").replace("...", ""))
        if len(compact_q) >= 6:
            spans = _find_compressed_spans(text, compact_q)
            if spans:
                # 压缩命中：坐标取首处，核验标 ambiguous（位置不唯一）或
                # verified（唯一命中）；end 由 offset 表回推真实终点
                s, e = spans[0]
                return s, e, ("ambiguous" if len(spans) > 1 else "verified")
            return None, None, "missing"
    if not positions:
        return None, None, "missing"
    if len(positions) > 1:
        # 多命中：坐标取首处，核验标 ambiguous（位置不唯一）
        s = positions[0]
        return s, s + len(bare), "ambiguous"
    s = positions[0]
    return s, s + len(bare), "verified"


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


def _canon_for_compare(t: str) -> str:
    """坐标校验用的规范化：去首尾装饰与全部空白（与 locate 同口径）。"""
    return re.sub(r"\s+", "", (t or "").strip().strip(_QUOTE_TRIM).strip())


def normalize_evidence_ref(ref: dict[str, Any], text: str) -> dict[str, Any]:
    """票据归一化（第三轮审计 P2）：历史票据补定位、清非资格 ID、重算合格 ID。

    - verified/ambiguous 但缺坐标：用 text 重新定位（与新生成票据同锚定，
      否则旧表示与新表示哈希不同，跨层去重失效）；
    - verified/ambiguous 坐标不实（批 2b-① 终审钉 4 的迁移路径）：老票据
      的压缩匹配端点曾是估算值——校验 text[start:end] 与摘句的规范化相等，
      不实则重新定位（真实端点），ID 随坐标重算（id_recomputed 警告可见）；
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
    elif ref.get("verification") in ("verified", "ambiguous"):
        # 批 2b-① 坐标精确性校验：老票据的估算端点在此迁移为真实端点。
        # 只对**有坐标**的合格票做切片比对（上面的缺坐标分支已覆盖重定位）
        s = ref.get("start")
        e = ref.get("end")
        assert isinstance(s, int) and isinstance(e, int)  # 首分支已排除非 int
        t = text or ""
        slice_ok = (
            isinstance(s, int) and isinstance(e, int)
            and 0 <= s < e <= len(t)
            and _canon_for_compare(t[s:e]) == _canon_for_compare(ref.get("quote") or "")
        )
        if not slice_ok:
            s2, e2, loc = locate_quote_span(t, ref.get("quote") or "")
            if loc in ("verified", "ambiguous"):
                ref["start"], ref["end"], ref["verification"] = s2, e2, loc
            else:
                # 坐标不实且重定位失败：无法证明原文存在，降级（fail-closed）
                ref["verification"] = "missing"
                ref["start"] = None
                ref["end"] = None
        else:
            # PR review P2-b：端点归一——canon 相等可能来自尾部句读装饰
            # （如 quote「甲方付款，」配坐标 end 多含一个逗号），须与 locate
            # 同口径截到 bare 的精确终点，否则同引用因路径不同产生两个 ID
            bare = (ref.get("quote") or "").strip().strip(_QUOTE_TRIM).strip()
            if bare and t[s:s + len(bare)] == bare and e != s + len(bare):
                ref["end"] = s + len(bare)
    if ref.get("verification") in ("verified", "ambiguous"):
        # 2b-① canonical 化：quote 统一取原文切片——同一 span 的任何措辞
        # 变体（多写/少写标点、截断装饰）收敛到同一 ID（哈希的 quote 项
        # 一致），这是「span 复用」的实质机制；切片随时可从 text 重建，
        # 缓存/索引无需保存合同原文（审计钉：缓存只存坐标和 ID）。
        s, e = ref.get("start"), ref.get("end")
        if isinstance(s, int) and isinstance(e, int) and 0 <= s < e <= len(text or ""):
            canonical_quote = (text or "")[s:e][:300]
            if canonical_quote and ref.get("quote") != canonical_quote:
                ref["quote"] = canonical_quote
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


def _find_compressed_spans(text: str, needle: str, limit: int = 20) -> list[tuple[int, int]]:
    """压缩匹配（忽略空白）并返回原文**真实区间** [(start, end), ...]。

    批 2b-①（终审钉 4）：end 由压缩文本的 offset 表回推——
    命中区间在原文中的真实终点 = 第 len(needle)-1 个非空白字符的原文下标 + 1，
    保证 text[start:end] 去空白后 == needle（不再按摘句长度估算）。
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
    spans: list[tuple[int, int]] = []
    idx = compressed.find(needle)
    while idx >= 0 and len(spans) < limit:
        start = offsets[idx]
        end = offsets[idx + len(needle) - 1] + 1
        spans.append((start, end))
        idx = compressed.find(needle, idx + 1)
    return spans


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
    span_ids: dict[str, set[str]] = {}  # 施工纪律 1：同 span 异 ID 检测

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
            if isinstance(ev.get("start"), int) and isinstance(ev.get("end"), int):
                span_key = (
                    f"{ev.get('document_version') or ''}|{ev.get('clause_id') or ''}"
                    f"|{ev.get('start')}|{ev.get('end')}"
                )
                span_ids.setdefault(span_key, set()).add(eid)
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
        # broken 账目三个来源：①归一化改写前捕获 ②登记簿自检（盲区）
        # ③同 span 异 ID（施工纪律 1——canonical 收敛后本不应出现，出现即
        # 说明有绕过归一化的写入，固定收敛到字典序最小合法 ID）
        "broken_ref_count": len(warnings) + len(registry_broken)
        + sum(max(0, len(ids) - 1) for ids in span_ids.values()),
        "broken_refs": (list(warnings) + registry_broken + [
            {"where": key, "old_evidence_id": min(ids),
             "new_evidence_id": "", "reason": "duplicate_span"}
            for key, ids in sorted(span_ids.items()) if len(ids) > 1
        ])[:_BROKEN_REFS_SAMPLE_CAP],
    }


# ---------- 批 2b-①：统一发证窗口 + span 索引（可重建缓存） ----------

EVIDENCE_INDEX_VERSION = 1


def resolve_or_build_evidence(
    *,
    text: str,
    quote: str,
    parse_source: ParseSource,
    document_version: str,
    clause_id: Optional[str] = None,
    start: Optional[int] = None,
    end: Optional[int] = None,
    clause_index: Optional[dict[str, Any]] = None,
    index: Optional[dict[str, Any]] = None,
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """统一发证窗口（批 2b-①）：建票 → 归一化收敛 → span 索引登记。

    七层产票的收口入口：票据先经 normalize_evidence_ref（canonical 化
    quote=原文切片 + 坐标校验），同 span 必然同 ID；index（可重建缓存，
    by_span 主键——审计第二轮钉 1）命中即复用、异 ID 记 duplicate_span
    警告并按施工纪律固定收敛。返回 (ticket, warnings)。

    index 形状（只存坐标和 ID，不保存合同原文——切片随时可从 text 重建）：
        {"version": 1, "by_span": {"dv|clause|start|end": "ev-..."}}
    """
    warnings: list[dict[str, str]] = []
    ticket = normalize_evidence_ref(
        build_evidence(
            text=text, quote=quote, parse_source=parse_source,
            document_version=document_version, clause_id=clause_id,
            start=start, end=end, clause_index=clause_index,
        ),
        text or "",
    )
    if index is None:
        return ticket, warnings
    index.setdefault("version", EVIDENCE_INDEX_VERSION)
    index.setdefault("by_span", {})
    s, e = ticket.get("start"), ticket.get("end")
    if ticket.get("verification") in ("verified", "ambiguous") and (
        isinstance(s, int) and isinstance(e, int)
    ):
        key = f"{document_version}|{ticket.get('clause_id') or ''}|{s}|{e}"
        existing = index["by_span"].get(key)
        if existing and existing != ticket["evidence_id"]:
            # 施工纪律 1：同 span 异 ID（旧数据混合）——固定收敛到字典序
            # 最小合法 ID，绝不定不出或随遍历顺序漂移
            warnings.append({
                "where": key,
                "old_evidence_id": str(existing),
                "new_evidence_id": str(ticket["evidence_id"]),
                "reason": "duplicate_span",
            })
            # PR review P2-a：索引必须与返回票一致——遗留 ID 无法重建内容，
            # 返回票以内容自洽的新 ID 为准（evidence_id == 内容哈希是更高
            # 级不变量）；纪律 1 的字典序收敛保留在 rebuild 路径（候选都是
            # 真实票据、个个内容自洽，见 rebuild_evidence_index）
            index["by_span"][key] = str(ticket["evidence_id"])
        else:
            index["by_span"][key] = str(ticket["evidence_id"])
    return ticket, warnings


def rebuild_evidence_index(row_normalized: dict[str, Any]) -> dict[str, Any]:
    """从六容器票据全量重建 span 索引（可重建缓存的「重建」半边）。

    纯读派生：产出**响应/合并副本**所需的新 dict——GET 读路径绝不写回
    store 行（审计修订六红线）；写方锁内合并（_merge_evidence_index）
    显式落库属 §1.6-4 授权。同 span 多 ID（canonical 收敛后理论不可达，
    防御保留）→ 取字典序最小合法 ID 为 canonical，其余记 _duplicates。
    """
    by_span: dict[str, str] = {}
    duplicates: list[dict[str, str]] = []
    text = row_normalized.get("text") or ""
    dv = row_normalized.get("document_version") or ""

    def _reg(ev: Any) -> None:
        if not isinstance(ev, dict) or ev.get("verification") not in (
            "verified", "ambiguous"
        ):
            return
        s, e = ev.get("start"), ev.get("end")
        eid = str(ev.get("evidence_id") or "")
        if not (isinstance(s, int) and isinstance(e, int) and eid):
            return
        key = f"{dv}|{ev.get('clause_id') or ''}|{s}|{e}"
        existing = by_span.get(key)
        if existing is None:
            by_span[key] = eid
        elif existing != eid:
            duplicates.append({
                "where": key,
                "old_evidence_id": existing,
                "new_evidence_id": eid,
                "reason": "duplicate_span",
            })
            by_span[key] = min(existing, eid)

    for it in row_normalized.get("items") or []:
        _reg(it.get("evidence") if isinstance(it, dict) else None)
    for cand in row_normalized.get("blind_candidates") or []:
        _reg(cand.get("evidence") if isinstance(cand, dict) else None)
    quality = row_normalized.get("quality") or {}
    if isinstance(quality, dict):
        for obs in quality.get("observations") or []:
            _reg(obs.get("evidence") if isinstance(obs, dict) else None)
        for f in quality.get("facts") or []:
            _reg(f.get("evidence") if isinstance(f, dict) else None)
    for f in row_normalized.get("facts") or []:
        _reg(f.get("evidence") if isinstance(f, dict) else None)
    objections = row_normalized.get("objections") or {}
    if isinstance(objections, dict):
        for ob in objections.get("objections") or []:
            _reg(ob.get("evidence") if isinstance(ob, dict) else None)
    verify = row_normalized.get("verify") or {}
    if isinstance(verify, dict):
        for q in verify.get("questions") or []:
            _reg(q.get("evidence") if isinstance(q, dict) else None)
    return {"version": EVIDENCE_INDEX_VERSION, "by_span": by_span,
            "_duplicates": duplicates, "_text_len": len(text)}


# ---------- 批 2b-②：主张标注（claim_id / claim_content_hash / evidence_refs） ----------

_CLAIM_SCHEMA_VERSION = "cc1"
_CLAIM_PREFIX = "cl-"
_CONTENT_PREFIX = "cc-"

# verify.source 白名单（审计非阻塞项：严格白名单，非法值不入身份）
_VERIFY_SOURCE_WHITELIST = frozenset({"quality_obs", "blind", "pending", "fact", "rule_attention"})
# quality.dimension 白名单
_QUALITY_DIMENSION_WHITELIST = frozenset({"completeness", "consistency", "impact"})


def rule_pack_versions(category: str) -> dict[str, str]:
    """规则包版本三元组的两个内容哈希（批 2b-② v1.8 字节级规范）。

    - rule_pack_content_version：该品类 checklist YAML 文件原始字节哈希
      （注释/格式变化也算——确定性优先，宁可信版本敏感）
    - rule_engine_version：checklist.py 引擎代码文件原始字节哈希
      （改代码没改 YAML 的行为变化同样逃不过版本号）
    - 品类未知时回落 procurement（与 load_checklist 兜底同口径）
    """
    from pathlib import Path

    base = Path(__file__).resolve().parents[2] / "config"
    config_path = base / ("checklist_" + category + ".yaml")
    if not config_path.exists():
        config_path = base / "checklist_procurement.yaml"
    engine_path = Path(__file__).resolve().parent / "checklist.py"
    return {
        "rule_pack_id": category,
        "rule_pack_content_version": hashlib.sha256(config_path.read_bytes()).hexdigest()[:12],
        "rule_engine_version": hashlib.sha256(engine_path.read_bytes()).hexdigest()[:12],
    }


def derive_claim_id(
    *, document_version: str, analysis_scope: str, claim_type: str, business_key: str
) -> str:
    """主张编号（批 2b-②）：文档 + 分析范围（规则包三元组）+ 类型 + 业务键。"""
    blob = chr(31).join([document_version, analysis_scope, claim_type, business_key])
    return _CLAIM_PREFIX + hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


def _escape_content_value(v: str) -> str:
    """值内分隔符可逆转义（v1.4）：反斜杠与 <US>/<RS> 控制字符，其余原样保留。"""
    return (
        (v or "").replace("\\", "\\\\")
        .replace(chr(31), "\\u001f")
        .replace(chr(30), "\\u001e")
    )


# 批 2b-② §2.2：各主张类型的固定字段顺序（审计 P2——字母序违背规范）
_CONTENT_FIELD_ORDER: dict[str, list[str]] = {
    "rule_item": ["name", "note"],
    "blind_candidate": ["name", "note"],
    "quality_observation": ["title", "comment"],
    "verify_question": ["question", "title"],
    "objection": ["legal_reasoning", "proposal", "stance_check"],
}


def claim_content_hash(claim_type: str, records: list[dict[str, str]]) -> str:
    """主张内容指纹（批 2b-② §2）：完整记录排序聚合，字节级序列化。

    - 记录内字段按白名单固定顺序、带字段名（title/comment 互换 hash 必变）
    - 组聚合排序单位是完整记录（字段对应关系不丢）
    - 单条主张 = 组大小 1，走同一路径（T5k 逐字节相等）
    """
    field_order = _CONTENT_FIELD_ORDER.get(claim_type, [])
    serialized_records = []
    for rec in records:
        # 固定字段顺序（白名单优先），白名单外字段排后（防御，正常不出现）
        ordered = [f for f in field_order if f in rec] + sorted(
            k for k in rec.keys() if k not in field_order
        )
        parts = [f + "=" + _escape_content_value(rec[f] or "") for f in ordered]
        serialized_records.append(chr(31).join(parts))
    serialized_records.sort()
    serialized = (
        "schema_version=" + _CLAIM_SCHEMA_VERSION + chr(31) + "records="
        + chr(30).join(serialized_records)
    )
    return _CONTENT_PREFIX + hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:12]


def annotate_review_claims(
    row_normalized: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """主张标注（批 2b-② 主入口）：claim_id / claim_content_hash / evidence_refs。

    纯函数——只改传入的响应副本（pipeline 传规范化副本、get_review 传深拷贝），
    绝不写回 store。claim_id 派生自**已收敛票据**（2b-① 之后），写/读双路径同源。
    旧记录 scope 缺失 → "legacy" + 迁移警告（返回值 warnings，由调用方决定
    去向：get_review 放响应字段，绝不写回 store——实现稿 §1.4 写入语义）。
    """
    warnings: list[dict[str, str]] = []
    rp = row_normalized.get("rule_pack")
    if isinstance(rp, dict) and rp.get("rule_pack_id"):
        scope = chr(31).join([
            str(rp.get("rule_pack_id") or ""),
            str(rp.get("rule_pack_content_version") or ""),
            str(rp.get("rule_engine_version") or ""),
        ])
    else:
        scope = "legacy"
        warnings.append({
            "where": "row.rule_pack",
            "reason": "legacy_scope",
            "detail": "旧记录缺规则包版本，claim_id 以 legacy scope 派生",
        })
    dv = row_normalized.get("document_version") or ""

    def _valid_primary(obj: dict[str, Any]) -> Optional[str]:
        ev = obj.get("evidence")
        if not isinstance(ev, dict):
            return None
        if ev.get("verification") not in _REGISTRY_QUALIFIED:
            return None
        eid = str(ev.get("evidence_id") or "")
        return eid if _ID_SHAPE.fullmatch(eid) else None

    def _refs(primary: Optional[str], extra: list[tuple[str, str]]) -> list[dict[str, str]]:
        refs: list[dict[str, str]] = []
        if primary:
            refs.append({"evidence_id": primary, "relation": "primary"})
        for e, r in extra:
            if e:
                refs.append({"evidence_id": e, "relation": r})
        deduped = {(_r["evidence_id"], _r["relation"]): _r for _r in refs}
        out = list(deduped.values())
        out.sort(key=lambda _r: (_r["relation"], _r["evidence_id"]))
        return out

    def _claim(
        obj: dict[str, Any], claim_type: str, business_key: str,
        content: dict[str, str], primary: Optional[str],
        extra_refs: list[tuple[str, str]],
    ) -> None:
        if primary:
            obj["claim_id"] = derive_claim_id(
                document_version=dv, analysis_scope=scope,
                claim_type=claim_type, business_key=business_key,
            )
            obj["evidence_refs"] = _refs(primary, extra_refs)
        else:
            # 约束 4：无合格主证据不发正式编号（空串不参与身份计算）
            obj["claim_id"] = ""
            obj["evidence_refs"] = []
        obj["claim_content_hash"] = claim_content_hash(claim_type, [content])

    def _key(*parts: Any) -> str:
        return chr(31).join(str(x) for x in parts)

    # --- rule_item ---
    for it in row_normalized.get("items") or []:
        if not isinstance(it, dict):
            continue
        primary = _valid_primary(it)
        _claim(
            it, "rule_item", _key(it.get("id"), primary),
            {"name": str(it.get("name") or ""), "note": str(it.get("note") or "")},
            primary, [],
        )

    # --- blind_candidate ---
    for c in row_normalized.get("blind_candidates") or []:
        if not isinstance(c, dict):
            continue
        primary = _valid_primary(c)
        _claim(
            c, "blind_candidate", _key(c.get("id"), primary),
            {"name": str(c.get("name") or ""), "note": str(c.get("note") or "")},
            primary, [],
        )

    # --- quality_observation（dimension+evidence 同键多条 = 同一主张，组聚合指纹） ---
    quality = row_normalized.get("quality") or {}
    if isinstance(quality, dict):
        groups: dict[str, list[dict[str, Any]]] = {}
        group_order: list[str] = []
        for obs in quality.get("observations") or []:
            if not isinstance(obs, dict):
                continue
            primary = _valid_primary(obs)
            dim = str(obs.get("dimension") or "")
            if dim not in _QUALITY_DIMENSION_WHITELIST or not primary:
                obs["claim_id"] = ""
                obs["evidence_refs"] = []
                obs["claim_content_hash"] = claim_content_hash(
                    "quality_observation",
                    [{"title": str(obs.get("title") or ""), "comment": str(obs.get("comment") or "")}],
                )
                continue
            gk = _key(dim, primary)
            if gk not in groups:
                groups[gk] = []
                group_order.append(gk)
            groups[gk].append(obs)
        for gk in group_order:
            members = groups[gk]
            group_claim = derive_claim_id(
                document_version=dv, analysis_scope=scope,
                claim_type="quality_observation", business_key=gk,
            )
            group_hash = claim_content_hash(
                "quality_observation",
                [{"title": str(m.get("title") or ""), "comment": str(m.get("comment") or "")}
                 for m in members],
            )
            for m in members:
                m["claim_id"] = group_claim
                m["claim_content_hash"] = group_hash
                m["evidence_refs"] = _refs(_valid_primary(m), [])

    # --- verify_question（source_subject_key；pending 无稳定对象键不发正式 ID） ---
    verify = row_normalized.get("verify") or {}
    if isinstance(verify, dict):
        for q in verify.get("questions") or []:
            if not isinstance(q, dict):
                continue
            source = str(q.get("source") or "")
            primary = _valid_primary(q)
            content = {
                "question": str(q.get("question") or ""),
                "title": str(q.get("title") or ""),
            }
            if (
                source not in _VERIFY_SOURCE_WHITELIST
                or source == "pending"
                or not primary
            ):
                # 非法来源 / pending（无稳定对象键）/ 无合格证据：不发正式编号
                q["claim_id"] = ""
                q["evidence_refs"] = []
                q["claim_content_hash"] = claim_content_hash("verify_question", [content])
                continue
            ssk = str(q.get("source_subject_key") or "")
            _claim(q, "verify_question", _key(source, ssk, primary), content, primary, [])

    # --- objection（含 rebuts fail-closed；content 含 stance_check） ---
    objections = row_normalized.get("objections") or {}
    items_by_id = {
        str(it.get("id")): it
        for it in (row_normalized.get("items") or [])
        if isinstance(it, dict)
    }
    if isinstance(objections, dict):
        for ob in objections.get("objections") or []:
            if not isinstance(ob, dict):
                continue
            primary = _valid_primary(ob)
            accepted = ob.get("accepted") is True
            _claim(
                ob, "objection",
                _key(ob.get("item_id"), ob.get("direction"), primary),
                {
                    "legal_reasoning": str(ob.get("legal_reasoning") or ""),
                    "proposal": str(ob.get("proposal") or ""),
                    "stance_check": str(ob.get("stance_check") or ""),
                },
                primary, [],
            )
            if not accepted:
                ob["rebuts_status"] = "not_applicable"
                ob["rebuts_reason"] = ""
                continue
            target = items_by_id.get(str(ob.get("item_id") or ""))
            target_ev = _valid_primary(target) if isinstance(target, dict) else None
            if target_ev and ob.get("claim_id"):
                ob["rebuts_status"] = "present"
                ob["rebuts_reason"] = ""
                ob["evidence_refs"] = _refs(primary, [(target_ev, "rebuts")])
            else:
                # 阻塞三：不生成边（绝不伪造空 ID 假链接）——区分谁缺证据
                # （门禁 P3-2：诊断字段不得语义失真）
                ob["rebuts_status"] = "missing"
                ob["rebuts_reason"] = (
                    "self_no_valid_evidence" if not primary else "target_no_valid_evidence"
                )
    return row_normalized, warnings
