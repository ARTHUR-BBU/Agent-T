"""LangGraph library pipeline: parse → checklist → (optional blind-spot) → ask stub.

Uses langgraph as a library only — not LangGraph Platform.
"""
from __future__ import annotations

from typing import Any, TypedDict

from langgraph.graph import END, StateGraph

from app.services.blind_spot import annotate_rule_items, run_blind_spot_pass
from app.services.checklist import run_checklist
from app.services.extract import extract_text


class ReviewState(TypedDict, total=False):
    filename: str
    raw_bytes: bytes
    category: str
    text: str
    items: list[dict[str, Any]]
    policies: list[str]
    category_label: str
    error: str
    blind_candidates: list[dict[str, Any]]
    blind_skipped_messages: list[str]
    blind_skipped_reason: str
    blind_enabled: bool


def node_parse(state: ReviewState) -> ReviewState:
    try:
        text = extract_text(state["filename"], state["raw_bytes"])
        return {"text": text, "error": ""}
    except Exception as exc:  # noqa: BLE001
        return {"text": "", "error": str(exc)}


def node_checklist(state: ReviewState) -> ReviewState:
    if state.get("error"):
        return {}
    result = run_checklist(state.get("text") or "", state.get("category") or "procurement")
    items = annotate_rule_items(result["items"])
    return {
        "items": items,
        "policies": result.get("policies") or [],
        "category_label": result.get("category_label") or "",
        "category": result.get("category") or state.get("category") or "procurement",
    }


def node_blind_spot(state: ReviewState) -> ReviewState:
    """Optional additive 补盲 pass; never mutates rule item statuses."""
    if state.get("error"):
        return {
            "blind_candidates": [],
            "blind_skipped_messages": [],
            "blind_skipped_reason": None,
            "blind_enabled": False,
        }
    out = run_blind_spot_pass(
        text=state.get("text") or "",
        items=state.get("items") or [],
        policies=state.get("policies") or [],
    )
    return {
        "blind_candidates": out.get("blind_candidates") or [],
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
    g.add_node("blind_spot", node_blind_spot)
    g.add_node("ask_ready", node_ask_ready)
    g.set_entry_point("parse")
    g.add_edge("parse", "checklist")
    g.add_edge("checklist", "blind_spot")
    g.add_edge("blind_spot", "ask_ready")
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


def run_review(filename: str, raw_bytes: bytes, category: str = "procurement") -> dict[str, Any]:
    graph = get_graph()
    final: ReviewState = graph.invoke(
        {
            "filename": filename,
            "raw_bytes": raw_bytes,
            "category": category or "procurement",
        }
    )
    return {
        "text": final.get("text") or "",
        "items": final.get("items") or [],
        "policies": final.get("policies") or [],
        "category": final.get("category") or category,
        "category_label": final.get("category_label") or category,
        "error": final.get("error") or "",
        "blind_candidates": final.get("blind_candidates") or [],
        "blind_skipped_messages": final.get("blind_skipped_messages") or [],
        "blind_skipped_reason": final.get("blind_skipped_reason"),
        "blind_enabled": bool(final.get("blind_enabled")),
    }
