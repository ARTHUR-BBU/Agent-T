"""补盲 (blind-spot) v2: targeted candidate proposals with quotes only.

M3.5 changes (定向补盲):
- Trigger surface narrowed: only 「未找到」 items + items the scorecard names
  as gaps (表述弱/评分点名缺口). No longer sweeps every 「通过」 item.
- Still additive only: candidates never touch rule statuses.
- No quote → skip with message 「缺少原文依据，已跳过」.
- Switch via BLIND_SPOT_ENABLED (default true; also 1/yes). When off: zero
  candidates, no 补盲 strings in payload; scorecard still runs.

The single merged LLM call lives in model_review.py; this module holds the
candidate normalization/validation helpers.
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

from app.services import scorecard
from app.services.checklist import STATUS_ATTENTION, STATUS_NA, STATUS_NOT_FOUND, STATUS_PASS

logger = logging.getLogger(__name__)

SKIP_NO_QUOTE = "缺少原文依据，已跳过"


def is_blind_spot_enabled() -> bool:
    """Default ON. Accept true / 1 / yes (case-insensitive)."""
    raw = os.getenv("BLIND_SPOT_ENABLED", "true")
    return str(raw).strip().lower() in ("true", "1", "yes", "on")


def annotate_rule_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Attach tag_source on rule results without changing status."""
    out: list[dict[str, Any]] = []
    for it in items:
        row = dict(it)
        # 需关注 from rules get explicit 「规则」 source; others stay null
        if row.get("status") == STATUS_ATTENTION:
            row["tag_source"] = "rule"
        else:
            row.setdefault("tag_source", None)
        out.append(row)
    return out


def select_target_gaps(
    items: list[dict[str, Any]],
    named_item_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    """定向补盲 v2：候选只允许落在这些空隙条目上。

    - 「未找到」条目：始终是靶点（缺项是硬信号）。
    - 「通过」条目：仅当评分卡点名（表述弱/缺口）时才是靶点。
    - 需关注 / 本类不适用：永远不是靶点（不重报、不覆盖）。
    """
    named = {str(i) for i in (named_item_ids or [])}
    gaps: list[dict[str, Any]] = []
    for it in items:
        if it.get("category_na") or it.get("status") == STATUS_NA:
            continue
        st = it.get("status")
        if st == STATUS_NOT_FOUND:
            gaps.append(it)
        elif st == STATUS_PASS and str(it.get("id")) in named:
            gaps.append(it)
    return gaps


def normalize_candidates(
    raw: str | list[Any],
    text: str,
    gaps: list[dict[str, Any]],
    named_source_ids: set[str] | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Validate model candidates against targeted gaps.

    Returns (candidates, skipped_messages). Additive only: never rewrites
    rule statuses; fake quotes are dropped.
    """
    gap_by_id = {str(g.get("id")): g for g in gaps}
    gap_by_name = {str(g.get("name")): g for g in gaps}
    parsed = raw if isinstance(raw, list) else _parse_candidates(raw)

    candidates: list[dict[str, Any]] = []
    skipped: list[str] = []
    seen_ids: set[str] = set()

    for row in parsed:
        if not isinstance(row, dict):
            continue
        item_id = str(row.get("item_id") or row.get("id") or "").strip()
        name = str(row.get("name") or "").strip()
        note = str(row.get("note") or "").strip()
        quote = str(row.get("quote") or "").strip()
        # strip decorative quotes around quote
        quote = quote.strip("「」\"'“”")

        base = gap_by_id.get(item_id) or gap_by_name.get(name)
        if not base:
            # unknown item — ignore (do not invent checklist rows)
            continue
        cid = str(base.get("id"))
        if cid in seen_ids:
            continue
        # Never target items already 需关注 by rules (defense in depth)
        if base.get("status") == STATUS_ATTENTION:
            continue

        if not quote:
            skipped.append(f"{base.get('name')}: {SKIP_NO_QUOTE}")
            continue
        # Prefer quotes that appear in contract; if not found, still skip (no fake row)
        if not _quote_supported(text, quote):
            skipped.append(f"{base.get('name')}: {SKIP_NO_QUOTE}")
            continue

        seen_ids.add(cid)
        # 候选说明也过禁语表：候选方向是报风险，但同责任敞口不例外（肉饼审计 P2）
        note = scorecard.scrub_forbidden(note)
        candidate = {
            "id": cid,
            "name": base.get("name") or name,
            "status": STATUS_ATTENTION,
            "note": note or "规则未标需关注，模型提出候选风险",
            "quote": quote,
            "tag_source": "blind",
            "needs_confirm": True,
            "hits": [],
        }
        # 评分卡点名的候选带独立标记（不改 tag_source 枚举）
        if named_source_ids and cid in named_source_ids:
            candidate["named_by_scorecard"] = True
        candidates.append(candidate)

    return candidates, skipped


def _quote_supported(text: str, quote: str) -> bool:
    if not quote or not text:
        return False
    if quote in text:
        return True
    # tolerate whitespace / ellipsis differences
    compact_q = re.sub(r"\s+", "", quote.replace("…", "").replace("...", ""))
    compact_t = re.sub(r"\s+", "", text)
    if len(compact_q) >= 6 and compact_q in compact_t:
        return True
    return False


def _parse_candidates(raw: str) -> list[Any]:
    if not raw:
        return []
    text = raw.strip()
    fence = re.match(r"^```(?:json)?\s*([\s\S]*?)\s*```$", text)
    if fence:
        text = fence.group(1).strip()
    try:
        obj = json.loads(text)
        if isinstance(obj, list):
            return obj
        if isinstance(obj, dict):
            for key in ("candidates", "items", "blind_candidates", "结果"):
                if isinstance(obj.get(key), list):
                    return obj[key]
            return [obj]
    except json.JSONDecodeError:
        # try to find a JSON array substring
        m = re.search(r"\[[\s\S]*\]", text)
        if m:
            try:
                obj = json.loads(m.group(0))
                if isinstance(obj, list):
                    return obj
            except json.JSONDecodeError:
                pass
    return []
