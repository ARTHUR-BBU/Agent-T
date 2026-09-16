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
ParseSource = Literal["rules", "blind", "quality", "ask", "fact"]

_QUOTE_TRIM = "「」\"'“”『』…."


class EvidenceRef(BaseModel):
    """证据票据：同一发现可追到文档版本 + 坐标。"""

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

    return EvidenceRef(
        document_version=document_version or document_version_for(text or ""),
        quote=(quote or "")[:300],
        start=s,
        end=e,
        clause_id=cid or None,
        verification=ver,
        parse_source=parse_source,
    ).to_dict()


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
        # 已有票据：只补 document_version / clause_id
        if not existing.get("document_version") and document_version:
            existing["document_version"] = document_version
        if not existing.get("clause_id"):
            primary = item.get("primary_clause_id") or (
                (item.get("clause_ids") or [None])[0]
            )
            if primary:
                existing["clause_id"] = primary
        item["evidence"] = existing
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
