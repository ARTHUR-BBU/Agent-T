"""M3.5 merged model pass: scorecard + targeted blind-spot in ONE LLM call.

Why one call: 定向补盲 targets gaps the scorecard names, so both halves need
the same context — and the admin-config principle 「审查流水线在 run_checklist
之后最多一次批量 LLM 调用」 still holds.

Entry: run_model_review(text, items, policies, category, chat_fn=None).

Gating matrix (tests assert all of these):
- BLIND_SPOT_ENABLED=false → scorecard still produced; zero 补盲 payload.
- no LLM key → scorecard unavailable(no_llm_key) + blind skipped(no_llm_key).
- rule items empty → scorecard unavailable (有规则结果才出分).
- forbidden phrase hit → retry once; second hit → degrade to score-only.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from app.services import blind_spot, llm_ask, scorecard
from app.services.checklist import STATUS_ATTENTION

logger = logging.getLogger(__name__)


def run_model_review(
    *,
    text: str,
    items: list[dict[str, Any]],
    policies: list[str] | None = None,
    category: str = "procurement",
    chat_fn: Optional[Any] = None,
) -> dict[str, Any]:
    """Run the merged pass. Returns scorecard + blind halves.

    Never mutates rule item statuses. Score is advisory only.
    """
    blind_on = blind_spot.is_blind_spot_enabled()

    result: dict[str, Any] = {
        "scorecard": scorecard.unavailable("not_attempted"),
        "blind_candidates": [],
        "blind_skipped_messages": [],
        "blind_skipped_reason": None,
        "blind_enabled": blind_on,
    }

    if not scorecard.has_rule_results(items):
        result["scorecard"] = scorecard.unavailable("no_rule_results")
        return result

    deepseek = llm_ask._deepseek_key()
    zhipu = llm_ask._zhipu_key()
    xai = llm_ask._xai_key()
    if not deepseek and not zhipu and not xai and chat_fn is None:
        result["scorecard"] = scorecard.unavailable("no_llm_key")
        result["blind_skipped_reason"] = "no_llm_key" if blind_on else None
        return result

    segments = scorecard.load_scorecard_config(category)["segments"]
    if not segments:
        # 旧品类没配 scorecard: 块 → 评分未开通，且不浪费 LLM 调用
        result["scorecard"] = scorecard.unavailable("no_scorecard_config")
        return result
    system = scorecard.build_system_prompt(segments, policies or [])
    user = scorecard.build_user_prompt(text or "", items)

    try:
        raw = _call_llm(zhipu, xai, system, user, chat_fn, deepseek=deepseek)
    except Exception:  # noqa: BLE001
        # reason 只给固定码：exc 含供应商 URL/响应体，会经 API 和 docx 报告外泄（肉饼门禁 P2-2）
        logger.exception("Model-review LLM error")
        result["scorecard"] = scorecard.unavailable("llm_error")
        return result

    payload = scorecard.parse_model_payload(raw)
    degraded = False

    # 禁语命中或解析失败 → 重试一次（更严格提醒）；再命中 → 降级为仅展示分数
    if payload is None or scorecard.check_forbidden(_scorecard_text(payload)):
        retry_system = system + "\n\n【再次提醒】上一轮输出包含禁止表述或结构错误。重新输出，严禁出现任何整体性背书/推翻规则档位的表述，只输出 JSON。"
        try:
            raw = _call_llm(zhipu, xai, retry_system, user, chat_fn, deepseek=deepseek)
        except Exception:  # noqa: BLE001
            logger.exception("Model-review retry LLM error")
            result["scorecard"] = scorecard.unavailable("llm_error")
            return result
        payload = scorecard.parse_model_payload(raw)
        if payload is None:
            result["scorecard"] = scorecard.unavailable("parse_failed")
            return result
        if scorecard.check_forbidden(_scorecard_text(payload)):
            degraded = True

    final = scorecard.postprocess(payload, items, segments)
    if degraded or not scorecard.naming_complete(final, items):
        final = scorecard.degrade_to_score_only(final)
    result["scorecard"] = final

    # ---- candidates half (定向补盲 v2) ----
    named_ids = _named_gap_ids(payload)
    if not blind_on:
        result["blind_skipped_reason"] = None
        return result

    gaps = blind_spot.select_target_gaps(items, named_ids)
    if not gaps:
        return result

    candidates, skipped = blind_spot.normalize_candidates(
        (payload or {}).get("candidates") or [],
        text or "",
        gaps,
        named_source_ids=set(named_ids),
    )
    result["blind_candidates"] = candidates
    result["blind_skipped_messages"] = skipped
    return result


def _named_gap_ids(payload: Optional[dict[str, Any]]) -> list[str]:
    """Item ids the scorecard names as gaps (表述弱/缺口) — blind v2 targets."""
    if not payload:
        return []
    named: list[str] = []
    sc = payload.get("scorecard") or {}
    for key in ("gap_item_ids", "named_gaps"):
        v = sc.get(key)
        if isinstance(v, list):
            named.extend(str(x) for x in v)
    for s in sc.get("segments") or []:
        if isinstance(s, dict) and isinstance(s.get("gap_item_ids"), list):
            named.extend(str(x) for x in s["gap_item_ids"])
    return named


def _scorecard_text(payload: Optional[dict[str, Any]]) -> str:
    """Flatten the scorecard half for forbidden-phrase scanning."""
    if not payload:
        return ""
    sc = payload.get("scorecard") or {}
    parts = [str(sc.get("summary") or "")]
    for s in sc.get("segments") or []:
        if isinstance(s, dict):
            parts.append(str(s.get("comment") or ""))
    return " ".join(parts)


def _call_llm(zhipu: Optional[str], xai: Optional[str], system: str, user: str, chat_fn: Optional[Any],
              deepseek: Optional[str] = None) -> str:
    if chat_fn is not None:
        return chat_fn(system, user)
    if deepseek:
        return llm_ask._chat_deepseek(deepseek, system, user)
    if zhipu:
        return llm_ask._chat_zhipu(zhipu, system, user)
    return llm_ask._chat_xai(xai, system, user)  # type: ignore[arg-type]
