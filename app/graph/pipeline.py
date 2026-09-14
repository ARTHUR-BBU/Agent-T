"""LangGraph library pipeline: parse → checklist → merged model pass → 质量层。

M3.5: model pass = scorecard + targeted blind-spot (model_review)。
阶段 1.2 起调用结构分两档：短合同单次合并调用；长合同 map-reduce 分段
阅读（详见 model_review 模块 docstring）。阶段 2.1 追加第 4 节点 quality
（AI 质量分析：三维度参考观察，铁律 5——不计分、不改档位）。
Uses langgraph as a library only — not LangGraph Platform.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, TypedDict

from langgraph.graph import END, StateGraph

from app.services import quality as quality_service
from app.services.blind_spot import annotate_rule_items
from app.services.checklist import run_checklist
from app.services.clause_index import build_clause_index, map_items_to_clauses
from app.services.evidence import attach_evidence_to_item, document_version_for
from app.services import facts as facts_service
from app.services import verify as verify_service
from app.services.extract import ExtractionError, extract_text
from app.services.model_review import run_model_review


class ReviewState(TypedDict, total=False):
    filename: str
    raw_bytes: bytes
    category: str
    # A4：用户声明立场（仅透传 quality/ask 解释；不进 checklist）
    stance: str
    text: str
    items: list[dict[str, Any]]
    policies: list[str]
    category_label: str
    error: str
    scorecard: dict[str, Any]
    blind_candidates: list[dict[str, Any]]
    blind_skipped_messages: list[str]
    blind_skipped_reason: str
    blind_enabled: bool
    # 阶段 2.1 质量层：quality_service.run_quality 产物（QualityInfo 形状 dict）
    quality: dict[str, Any]
    # 阶段 0.5：单次审查 LLM 预算对象（llm_budget.ReviewBudget | None），
    # 由 upload 请求创建、贯穿预审与审查线程；放 state 仅为透传给 model_review
    budget: Any
    # 阶段 1.1：条款索引（clause_index.build_clause_index 产物），由 node_parse
    # 基于入库同款全文构建，坐标锚定该 text；纯展示增强，error 路径不产出
    clause_index: Any
    # 阶段 0.2 补课：stage 进度回调（Callable[[str], None] | None），由 routes
    # worker 注入（写 store.stage），pipeline 自身不感知持久化
    on_stage: Any
    # 架构 batch3 / A5：阶段性结果回调（completion, partial_fields）
    # completion ∈ rules_complete | ai_partial | fully_complete
    on_partial: Any
    # 上传预审已解析的全文：复用一次解析产物，避免 silent double-parse
    parsed_text: str
    document_version: str
    completion: str
    # A6 有界主动核验状态
    verify: dict[str, Any]


def _emit_stage(state: ReviewState, stage: str) -> None:
    """向 worker 回调上报进度；回调异常绝不影响审查主流程。"""
    cb = state.get("on_stage")
    if not cb:
        return
    try:
        cb(stage)
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).warning("on_stage callback failed for stage=%s", stage, exc_info=True)


def _emit_partial(state: ReviewState, completion: str, **fields: Any) -> None:
    """阶段结果落盘（A5）：规则完成 / AI 部分完成 / 全部完成。异常不影响主流程。"""
    cb = state.get("on_partial")
    if not cb:
        return
    try:
        cb(completion, fields)
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).warning(
            "on_partial callback failed for completion=%s", completion, exc_info=True
        )


def node_parse(state: ReviewState) -> ReviewState:
    # scanning 从解析入口起算：大 PDF 的 Docling 提取可达数十秒，算进「规则扫描」
    # 才不会让等待页卡在上一段（triage 在 routes 的预审阶段已是完成态）
    _emit_stage(state, "scanning")
    # A5：优先复用上传预审已解析全文，避免二次解析浪费
    reused = (state.get("parsed_text") or "").strip()
    try:
        if reused:
            text = reused
        else:
            text = extract_text(state["filename"], state["raw_bytes"])
        doc_ver = document_version_for(text)
        return {
            "text": text,
            "error": "",
            "clause_index": build_clause_index(text),
            "document_version": doc_ver,
        }
    except ExtractionError as exc:
        return {"text": "", "error": str(exc)}
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).exception("Unhandled extraction failure")
        return {"text": "", "error": "文档解析失败，请重新上传或转换格式后再试"}


def node_checklist(state: ReviewState) -> ReviewState:
    if state.get("error"):
        return {}
    text = state.get("text") or ""
    result = run_checklist(text, state.get("category") or "procurement")
    items = annotate_rule_items(result["items"])
    clause_index = state.get("clause_index") or {}
    if clause_index:
        map_items_to_clauses(items, clause_index, text)
    doc_ver = state.get("document_version") or document_version_for(text)
    # A1：规则命中挂统一证据引用（不改 status——Design B）
    for it in items:
        attach_evidence_to_item(
            it, text=text, parse_source="rules",
            document_version=doc_ver, clause_index=clause_index,
        )
    out = {
        "items": items,
        "policies": result.get("policies") or [],
        "category_label": result.get("category_label") or "",
        "category": result.get("category") or state.get("category") or "procurement",
        "document_version": doc_ver,
        "completion": "rules_complete",
    }
    # A5：规则完成即落盘，前端可显示「规则已出、AI 未完成」
    _emit_partial(
        state,
        "rules_complete",
        items=items,
        policies=out["policies"],
        category=out["category"],
        category_label=out["category_label"],
        text=text,
        clause_index=clause_index,
        document_version=doc_ver,
        text_preview=text[:500]
    )
    return out


def node_model_review(state: ReviewState) -> ReviewState:
    """Merged model pass: scorecard + targeted blind candidates.

    Additive only — never mutates rule item statuses; score is advisory.
    """
    _emit_stage(state, "scoring")
    if state.get("error"):
        return {
            # 解析已失败，与「有规则结果才出分」的 no_rule_results 门禁区分开（肉饼审查 P3-4）
            "scorecard": {"available": False, "reason": "error"},
            "blind_candidates": [],
            "blind_skipped_messages": [],
            "blind_skipped_reason": None,
            "blind_enabled": False,
        }
    try:
        out = run_model_review(
            text=state.get("text") or "",
            items=state.get("items") or [],
            policies=state.get("policies") or [],
            category=state.get("category") or "procurement",
            clause_index=state.get("clause_index"),
            budget=state.get("budget"),
        )
    except Exception:  # noqa: BLE001 — F03：模型坏输出不得抹掉已完成规则结果
        logging.getLogger(__name__).exception("Model review failed; keeping rule items")
        return {
            "scorecard": {"available": False, "reason": "incomplete_model_output"},
            "blind_candidates": [],
            "blind_skipped_messages": [],
            "blind_skipped_reason": "incomplete_model_output",
            "blind_enabled": False,
        }
    candidates = out.get("blind_candidates") or []
    # 补盲候选同样标注条款归属（它们正是「中段条款被点名」的主要载体）
    clause_index = state.get("clause_index") or {}
    if candidates and clause_index:
        map_items_to_clauses(candidates, clause_index, state.get("text") or "")
    # A1：补盲候选挂证据引用（status 仍为候选需确认，不碰规则档位）
    text = state.get("text") or ""
    doc_ver = state.get("document_version") or document_version_for(text)
    for cand in candidates:
        attach_evidence_to_item(
            cand, text=text, parse_source="blind",
            document_version=doc_ver, clause_index=clause_index,
        )
    payload = {
        "scorecard": out.get("scorecard") or {},
        "blind_candidates": candidates,
        "blind_skipped_messages": out.get("blind_skipped_messages") or [],
        "blind_skipped_reason": out.get("blind_skipped_reason"),
        "blind_enabled": bool(out.get("blind_enabled")),
        "completion": "ai_partial",
    }
    _emit_partial(
        state,
        "ai_partial",
        items=state.get("items") or [],
        scorecard=payload["scorecard"],
        blind_candidates=candidates,
        blind_skipped_messages=payload["blind_skipped_messages"],
        blind_skipped_reason=payload["blind_skipped_reason"],
        blind_enabled=payload["blind_enabled"]
    )
    return payload


def node_quality(state: ReviewState) -> ReviewState:
    """阶段 2.1 质量层：三维度参考观察。

    铁律 5：输出只进 quality 键，不计分、不改档位；任何失败软降级
    （QualityInfo.available=False），绝不影响既有链路。
    """
    if state.get("error") or not (state.get("text") or "").strip():
        # 解析已失败：quality 走 error 短路，语义不掩盖上游错误
        return {"quality": quality_service.outcome_unavailable("error")}
    # 只在真要跑时点亮 analyzing 段位——关闭态不让等待页闪过一段
    if quality_service.is_quality_enabled():
        _emit_stage(state, "analyzing")
    try:
        out = quality_service.run_quality(
            text=state.get("text") or "",
            items=state.get("items") or [],
            policies=state.get("policies") or [],
            category=state.get("category") or "procurement",
            stance=state.get("stance") or "neutral",
            clause_index=state.get("clause_index"),
            budget=state.get("budget"),
        )
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).exception("Quality pass failed")
        out = quality_service.outcome_unavailable("error")
    # 质量关闭/失败时仍落确定性事实材料（A2）；规则 items 绝不动（Design B）
    if not (out.get("facts") or []):
        text_body = state.get("text") or ""
        doc_ver = state.get("document_version") or document_version_for(text_body)
        seeded = facts_service.extract_deterministic_facts(
            text_body,
            document_version=doc_ver,
            clause_index=state.get("clause_index"),
        )
        out = {**out, "facts": seeded}
    facts_out = out.get("facts") or []
    # A6：规则/观察落定后跑一轮有界核验（确定性；不改档位）
    try:
        verify_out = verify_service.run_bounded_verify(
            text=state.get("text") or "",
            items=state.get("items") or [],
            quality=out,
            blind_candidates=state.get("blind_candidates") or [],
            facts=facts_out,
            clause_index=state.get("clause_index"),
            document_version=state.get("document_version") or "",
            prior=None,
        )
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).exception("Bounded verify failed")
        verify_out = verify_service.empty_verify(
            reason="error",
            document_version=state.get("document_version") or "",
        )
    _emit_partial(
        state,
        "fully_complete",
        quality=out,
        facts=facts_out,
        verify=verify_out,
    )
    return {
        "quality": out,
        "completion": "fully_complete",
        "verify": verify_out,
    }


def build_graph():
    g = StateGraph(ReviewState)
    g.add_node("parse", node_parse)
    g.add_node("checklist", node_checklist)
    g.add_node("model_review", node_model_review)
    g.add_node("quality", node_quality)
    g.set_entry_point("parse")
    g.add_edge("parse", "checklist")
    g.add_edge("checklist", "model_review")
    g.add_edge("model_review", "quality")
    g.add_edge("quality", END)
    return g.compile()


_graph = None


def get_graph():
    global _graph
    if _graph is None:
        _graph = build_graph()
    return _graph


def reset_graph() -> None:
    """Test helper: rebuild graph after code changes / env toggles."""
    global _graph
    _graph = None


def run_review(
    filename: str,
    raw_bytes: bytes,
    category: str = "procurement",
    budget: Any = None,
    on_stage: Callable[[str], None] | None = None,
    on_partial: Callable[[str, dict], None] | None = None,
    parsed_text: str | None = None,
    stance: str = "neutral",
) -> dict[str, Any]:
    graph = get_graph()
    final: ReviewState = graph.invoke(
        {
            "filename": filename,
            "raw_bytes": raw_bytes,
            "category": category or "procurement",
            "stance": stance or "neutral",
            "budget": budget,
            "on_stage": on_stage,
            "on_partial": on_partial,
            "parsed_text": parsed_text or "",
        }
    )
    return {
        "text": final.get("text") or "",
        "items": final.get("items") or [],
        "policies": final.get("policies") or [],
        "category": final.get("category") or category,
        "category_label": final.get("category_label") or category,
        "error": final.get("error") or "",
        "clause_index": final.get("clause_index"),
        "scorecard": final.get("scorecard") or {},
        "blind_candidates": final.get("blind_candidates") or [],
        "blind_skipped_messages": final.get("blind_skipped_messages") or [],
        "blind_skipped_reason": final.get("blind_skipped_reason"),
        "blind_enabled": bool(final.get("blind_enabled")),
        "quality": final.get("quality") or {},
        "document_version": final.get("document_version") or "",
        "completion": final.get("completion") or (
            "fully_complete" if not final.get("error") else ""
        ),
        "facts": (final.get("quality") or {}).get("facts") or [],
        "verify": final.get("verify") or {},
    }
