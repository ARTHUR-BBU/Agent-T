"""M3.5 merged model pass: scorecard + targeted blind-spot.

调用结构（阶段 1.2 起废除「最多一次批量 LLM 调用」旧约束）：
- 短合同（≤ MAX_CONTRACT_CHARS）：单次合并调用（评分+补盲同 payload），与
  历史版本逐字节一致——现有全部测试 fixture 走此路径。
- 长合同（> MAX_CONTRACT_CHARS）：map-reduce 分段阅读——每块一次 map 调用
  产出观察素材（≤ LLM_REVIEW_MAX_SEGMENTS 块，默认 4，不重试），再一次
  reduce 汇总调用（复用既有 system prompt 与输出 schema，禁语重试/降级/
  封顶全链不变）。补盲候选仍由 reduce payload 承载、_quote_supported 对
  全文校验（blind_spot 零改动）。

Entry: run_model_review(text, items, policies, category, chat_fn=None).

Gating matrix (tests assert all of these):
- BLIND_SPOT_ENABLED=false → scorecard still produced; zero 补盲 payload.
- no LLM key → scorecard unavailable(no_llm_key) + blind skipped(no_llm_key).
- rule items empty → scorecard unavailable (有规则结果才出分).
- forbidden phrase hit → retry once; second hit → degrade to score-only.
- budget exhausted → unavailable(budget_exceeded)（阶段 0.5，软降级同 llm_error）.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Optional

from app.services import blind_spot, llm_ask, scorecard
from app.services.checklist import STATUS_ATTENTION

logger = logging.getLogger(__name__)

# 分段阅读块数上限（env 调用时点读取，垃圾值回退默认——项目惯例）
DEFAULT_MAX_SEGMENTS = 4


def _max_segments() -> int:
    raw = (os.getenv("LLM_REVIEW_MAX_SEGMENTS", "") or "").strip()
    try:
        n = int(raw)
    except ValueError:
        return DEFAULT_MAX_SEGMENTS
    return n if n >= 1 else DEFAULT_MAX_SEGMENTS


def run_model_review(
    *,
    text: str,
    items: list[dict[str, Any]],
    policies: list[str] | None = None,
    category: str = "procurement",
    chat_fn: Optional[Any] = None,
    map_chat_fn: Optional[Any] = None,
    clause_index: Optional[dict[str, Any]] = None,
    budget: Optional[Any] = None,
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

    # —— 路径选择：≤6000 字走历史单调用路径（逐字节保留）；超长走分段阅读 ——
    observations: list[dict[str, Any]] = []
    map_gap_ids: list[str] = []
    segmented = len(text or "") > scorecard.MAX_CONTRACT_CHARS
    if segmented:
        observations, map_gap_ids, chunks_ok, chunks_total = _run_map_pass(
            text or "", items, policies or [], clause_index,
            deepseek, zhipu, xai, chat_fn, map_chat_fn, budget,
        )
        if chunks_ok == 0:
            # map 全败（异常/解析失败）：可用性优先于覆盖 → 回退头尾采样单调用。
            # 注意与「解析成功但无风险观察」区分——后者是合法结果，照走 reduce。
            logger.warning("Segmented map pass produced nothing; falling back to clipped single call")
            segmented = False

    if segmented:
        system = scorecard.build_system_prompt(segments, policies or [])
        user = scorecard.build_reduce_user_prompt(
            items, scorecard.format_observations(observations)
        )
    else:
        system = scorecard.build_system_prompt(segments, policies or [])
        user = scorecard.build_user_prompt(text or "", items)

    # 预算检查点（阶段 0.5）：调用前扣减，耗尽走软降级（同 llm_error 形态，
    # 不硬失败）。重试前同样检查——「第 1 次成功、重试时预算尽」也正确降级
    if budget is not None and not budget.try_consume():
        logger.warning("Model-review skipped: LLM budget exhausted")
        result["scorecard"] = scorecard.unavailable("budget_exceeded")
        result["blind_skipped_reason"] = "budget_exceeded" if blind_on else None
        return result

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
        if budget is not None and not budget.try_consume():
            logger.warning("Model-review retry skipped: LLM budget exhausted")
            result["scorecard"] = scorecard.unavailable("budget_exceeded")
            return result
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
    # 覆盖明示（外部审计二轮：不要让用户以为 AI 逐字读完了 8 万字合同；
    # 规则引擎仍全文扫描，模型参考层是预算内有限覆盖）
    if segmented:
        final["coverage"] = {
            "chunks_total": chunks_total,
            "chunks_reviewed": chunks_ok,
            "limited": chunks_ok < chunks_total,
        }
    result["scorecard"] = final

    # ---- candidates half (定向补盲 v2) ----
    # 分段路径：map 观察点名的缺口并入靶点（中段条款的「通过但表述弱」也值得补盲）
    named_ids = _named_gap_ids(payload) + map_gap_ids
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


def _run_map_pass(
    text: str,
    items: list[dict[str, Any]],
    policies: list[str],
    clause_index: Optional[dict[str, Any]],
    deepseek: Optional[str],
    zhipu: Optional[str],
    xai: Optional[str],
    chat_fn: Optional[Any],
    map_chat_fn: Optional[Any],
    budget: Optional[Any],
) -> tuple[list[dict[str, Any]], list[str], int, int]:
    """map 阶段：按块阅读产出观察素材。

    - 每块调用前预算检查，耗尽即停（已收观察直接进 reduce，reduce 额度优先）
    - 单块失败（异常/解析失败）不重试、跳过继续（延迟护栏，2026-09-07 教训）
    - 返回 (observations, map 点名的 gap item ids, 成功解析的块数, 总块数)
      ——第三项区分「全败回退」与「成功但无风险」；第四项供 coverage 明示
    """
    chunks = scorecard.build_review_chunks(text, clause_index, max_segments=_max_segments())
    if not chunks:
        return [], [], 0, 0
    system = scorecard.build_map_system_prompt(policies)
    total = len(chunks)
    observations: list[dict[str, Any]] = []
    gap_ids: list[str] = []
    chunks_ok = 0
    for part_no, chunk in enumerate(chunks, start=1):
        # 预算预留：给 reduce 留最后 1 次额度（remaining()==-1 表示不限）——
        # 否则段数调大时 map 会把预算吃光，长合同评分卡恒 unavailable（门禁 P3）
        if budget is not None:
            remaining = budget.remaining()
            if remaining >= 0 and remaining <= 1:
                logger.warning(
                    "Map pass stopped: reserving last budget credit for reduce (%d/%d chunks)", part_no - 1, total
                )
                break
            if not budget.try_consume():
                logger.warning("Map pass stopped early: LLM budget exhausted (%d/%d chunks)", part_no - 1, total)
                break
        user = scorecard.build_map_user_prompt(chunk, items, part_no, total)
        try:
            raw = _call_llm(zhipu, xai, system, user, map_chat_fn or chat_fn, deepseek=deepseek)
        except Exception:  # noqa: BLE001
            logger.exception("Map pass chunk %d/%d LLM error; skipping chunk", part_no, total)
            continue
        parsed = scorecard.parse_map_payload(raw)
        if parsed is None:
            # 调用成功但结构错误：该块按失败计，不跳过后续块
            logger.warning("Map pass chunk %d/%d returned unparseable payload", part_no, total)
            continue
        chunks_ok += 1
        for obs in parsed:
            observations.append(obs)
            ids = obs.get("gap_item_ids")
            if isinstance(ids, list):
                gap_ids.extend(str(g) for g in ids)
    return observations, gap_ids, chunks_ok, total


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
