"""Optional 补盲 (blind-spot) pass: LLM proposes 候选需关注 with quotes only.

Iron rules:
- Default checklist path unchanged (rules mark 通过/需关注/未找到/不适用).
- Blind only proposes candidates for gaps (not already 需关注 by rules).
- Never overwrite rule statuses; additive only.
- No quote → skip with message 「缺少原文依据，已跳过」.
- Switch via BLIND_SPOT_ENABLED (default true; also 1/yes). When off: zero candidates,
  no 补盲 strings in payload.
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Optional

from app.services import llm_ask

logger = logging.getLogger(__name__)

SKIP_NO_QUOTE = "缺少原文依据，已跳过"
STATUS_ATTENTION = "需关注"
STATUS_PASS = "通过"
STATUS_NOT_FOUND = "未找到"
STATUS_NA = "本类不适用"

# Gaps where blind may propose candidates (never rewrite existing 需关注 / N/A)
_GAP_STATUSES = {STATUS_PASS, STATUS_NOT_FOUND}


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


def run_blind_spot_pass(
    *,
    text: str,
    items: list[dict[str, Any]],
    policies: list[str] | None = None,
    chat_fn: Optional[Any] = None,
) -> dict[str, Any]:
    """Run at most one batched LLM call for blind candidates.

    Returns:
      {
        "blind_candidates": [...],
        "blind_skipped_messages": [...],  # e.g. 缺少原文依据
        "blind_skipped_reason": str | None,
        "blind_enabled": bool,
      }
    """
    if not is_blind_spot_enabled():
        return {
            "blind_candidates": [],
            "blind_skipped_messages": [],
            "blind_skipped_reason": None,
            "blind_enabled": False,
        }

    zhipu = llm_ask._zhipu_key()
    xai = llm_ask._xai_key()
    if not zhipu and not xai and chat_fn is None:
        return {
            "blind_candidates": [],
            "blind_skipped_messages": [],
            "blind_skipped_reason": "no_llm_key",
            "blind_enabled": True,
        }

    gaps = [
        it
        for it in items
        if it.get("status") in _GAP_STATUSES and not it.get("category_na")
    ]
    if not gaps:
        return {
            "blind_candidates": [],
            "blind_skipped_messages": [],
            "blind_skipped_reason": None,
            "blind_enabled": True,
        }

    system = _build_system_prompt(policies or [])
    user = _build_user_prompt(text or "", gaps)

    try:
        if chat_fn is not None:
            raw = chat_fn(system, user)
        elif zhipu:
            raw = llm_ask._chat_zhipu(zhipu, system, user)
        else:
            raw = llm_ask._chat_xai(xai, system, user)  # type: ignore[arg-type]
    except Exception as exc:  # noqa: BLE001
        logger.exception("Blind-spot LLM error")
        return {
            "blind_candidates": [],
            "blind_skipped_messages": [],
            "blind_skipped_reason": f"llm_error:{exc}",
            "blind_enabled": True,
        }

    return _normalize_llm_result(raw, text or "", gaps)


def _build_system_prompt(policies: list[str]) -> str:
    policy_block = "\n".join(f"- {p}" for p in policies) or "- （无额外政策）"
    return f"""你是合同审查「补盲」助手。规则引擎已打过标签；你只能对「规则未标需关注」的空隙项提出「候选需关注」。

硬性规则：
1. 只输出 JSON 数组，不要 markdown 围栏。
2. 每项必须含：item_id（须来自输入列表）、name、note、quote。
3. quote 必须是合同原文中可核对的连续摘录；没有原文依据就不要输出该项。
4. 禁止改写或覆盖规则引擎已有结论；禁止说「模型已判定通过/需关注」。
5. 只报真实风险候选；拿不准就省略。

政策参考：
{policy_block}
"""


def _build_user_prompt(text: str, gaps: list[dict[str, Any]]) -> str:
    body = text if len(text) <= 12000 else text[:12000] + "\n…(截断)"
    gap_lines = []
    for g in gaps:
        gap_lines.append(
            f"- id={g.get('id')} name={g.get('name')} status={g.get('status')} note={g.get('note') or ''}"
        )
    gaps_block = "\n".join(gap_lines)
    return f"""以下条目规则引擎标为「通过」或「未找到」（非需关注）。若你发现风险且能引用原文，请提出候选需关注。

空隙条目：
{gaps_block}

合同全文：
{body}

只输出 JSON 数组，元素形如：
{{"item_id":"...","name":"...","note":"...","quote":"..."}}
无候选则输出 []。
"""


def _normalize_llm_result(
    raw: str,
    text: str,
    gaps: list[dict[str, Any]],
) -> dict[str, Any]:
    gap_by_id = {str(g.get("id")): g for g in gaps}
    gap_by_name = {str(g.get("name")): g for g in gaps}
    parsed = _parse_candidates(raw)

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
        candidates.append(
            {
                "id": cid,
                "name": base.get("name") or name,
                "status": STATUS_ATTENTION,
                "note": note or "规则未标需关注，模型提出候选风险",
                "quote": quote,
                "tag_source": "blind",
                "needs_confirm": True,
                "hits": [],
            }
        )

    return {
        "blind_candidates": candidates,
        "blind_skipped_messages": skipped,
        "blind_skipped_reason": None,
        "blind_enabled": True,
    }


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
