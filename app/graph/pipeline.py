"""LangGraph library pipeline: parse → checklist → merged model pass → ask stub.

M3.5: model pass = scorecard + targeted blind-spot (model_review)。
阶段 1.2 起调用结构分两档：短合同单次合并调用；长合同 map-reduce 分段
阅读（详见 model_review 模块 docstring）。Uses langgraph as a library
only — not LangGraph Platform.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, TypedDict

from langgraph.graph import END, StateGraph

from app.services.blind_spot import annotate_rule_items
from app.services.checklist import run_checklist
from app.services.clause_index import build_clause_index, map_items_to_clauses
from app.services.extract import extract_text
from app.services.model_review import run_model_review


class ReviewState(TypedDict, total=False):
    filename: str
    raw_bytes: bytes
    category: str
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
    # 阶段 0.5：单次审查 LLM 预算对象（llm_budget.ReviewBudget | None），
    # 由 upload 请求创建、贯穿预审与审查线程；放 state 仅为透传给 model_review
    budget: Any
    # 阶段 1.1：条款索引（clause_index.build_clause_index 产物），由 node_parse
    # 基于入库同款全文构建，坐标锚定该 text；纯展示增强，error 路径不产出
    clause_index: Any
    # 阶段 0.2 补课：stage 进度回调（Callable[[str], None] | None），由 routes
    # worker 注入（写 store.stage），pipeline 自身不感知持久化
    on_stage: Any


def _emit_stage(state: ReviewState, stage: str) -> None:
    """向 worker 回调上报进度；回调异常绝不影响审查主流程。"""
    cb = state.get("on_stage")
    if not cb:
        return
    try:
        cb(stage)
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).warning("on_stage callback failed for stage=%s", stage, exc_info=True)


def node_parse(state: ReviewState) -> ReviewState:
    # scanning 从解析入口起算：大 PDF 的 Docling 提取可达数十秒，算进「规则扫描」
    # 才不会让等待页卡在上一段（triage 在 routes 的预审阶段已是完成态）
    _emit_stage(state, "scanning")
    try:
        text = extract_text(state["filename"], state["raw_bytes"])
        return {"text": text, "error": "", "clause_index": build_clause_index(text)}
    except Exception as exc:  # noqa: BLE001
        return {"text": "", "error": str(exc)}


def node_checklist(state: ReviewState) -> ReviewState:
    if state.get("error"):
        return {}
    result = run_checklist(state.get("text") or "", state.get("category") or "procurement")
    items = annotate_rule_items(result["items"])
    # 条款归属映射（阶段 1.1）：纯展示增强，失败静默降级为空映射，不影响档位
    clause_index = state.get("clause_index") or {}
    if clause_index:
        map_items_to_clauses(items, clause_index, state.get("text") or "")
    return {
        "items": items,
        "policies": result.get("policies") or [],
        "category_label": result.get("category_label") or "",
        "category": result.get("category") or state.get("category") or "procurement",
    }


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
    out = run_model_review(
        text=state.get("text") or "",
        items=state.get("items") or [],
        policies=state.get("policies") or [],
        category=state.get("category") or "procurement",
        clause_index=state.get("clause_index"),
        budget=state.get("budget"),
    )
    candidates = out.get("blind_candidates") or []
    # 补盲候选同样标注条款归属（它们正是「中段条款被点名」的主要载体）
    clause_index = state.get("clause_index") or {}
    if candidates and clause_index:
        map_items_to_clauses(candidates, clause_index, state.get("text") or "")
    return {
        "scorecard": out.get("scorecard") or {},
        "blind_candidates": candidates,
        "blind_skipped_messages": out.get("blind_skipped_messages") or [],
        "blind_skipped_reason": out.get("blind_skipped_reason"),
        "blind_enabled": bool(out.get("blind_enabled")),
    }


def node_ask_ready(state: ReviewState) -> ReviewState:
    """Marker node: Ask LLM is only invoked later via /api/ask on 需关注 items.

    Kept in the graph so the wiring parse→checklist→optional ask is explicit.
    """
    _ = state
    return {}


def build_graph():
    g = StateGraph(ReviewState)
    g.add_node("parse", node_parse)
    g.add_node("checklist", node_checklist)
    g.add_node("model_review", node_model_review)
    g.add_node("ask_ready", node_ask_ready)
    g.set_entry_point("parse")
    g.add_edge("parse", "checklist")
    g.add_edge("checklist", "model_review")
    g.add_edge("model_review", "ask_ready")
    g.add_edge("ask_ready", END)
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
) -> dict[str, Any]:
    graph = get_graph()
    final: ReviewState = graph.invoke(
        {
            "filename": filename,
            "raw_bytes": raw_bytes,
            "category": category or "procurement",
            "budget": budget,
            "on_stage": on_stage,
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
    }
