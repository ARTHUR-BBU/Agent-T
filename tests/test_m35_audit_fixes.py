"""肉饼 M3.5 合规审查（P1/P2/P3）整改的回归测试.

P1: prompt schema 必须真的要求模型输出 gap_item_ids（否则定向补盲点名是死路）.
P2-1: 「未找到」同触发封顶 89/74，不得出现「挂着未找到却 90+」.
P2-2: 封顶消息点名全部硬伤条款（含非核心段），不只列核心段.
P3-4: pipeline 解析失败短路时 reason=error，不冒充 no_rule_results.
"""
from __future__ import annotations

import json

from app.graph.pipeline import node_model_review
from app.services import scorecard
from app.services.model_review import _named_gap_ids


def _payload(raw_dict):
    return json.dumps(raw_dict, ensure_ascii=False)


def _model_payload(total, seg_scores, summary="汇总。", comments=None, seg_extra=None):
    segs = []
    for key, sc in seg_scores.items():
        c = {"key": key, "score": sc}
        if comments and key in comments:
            c["comment"] = comments[key]
        if seg_extra and key in seg_extra:
            c.update(seg_extra[key])
        segs.append(c)
    return {
        "scorecard": {"summary": summary, "segments": segs},
        "candidates": [],
    }


# ---------- P1: prompt schema 必须包含 gap_item_ids ----------

def test_system_prompt_schema_requests_gap_item_ids():
    """schema 没要 gap_item_ids → 模型永远不会给 → 定向补盲点名是死路（P1）."""
    segments = scorecard.load_scorecard_config("procurement")["segments"]
    prompt = scorecard.build_system_prompt(segments, ["政策一"])
    assert "gap_item_ids" in prompt
    assert "补盲" in prompt  # 点名用途要说清楚


def test_named_gap_ids_reads_segment_level_field():
    """_named_gap_ids 必须能读到段级 gap_item_ids（生产主路径）."""
    payload = _model_payload(
        90, {"A": 12, "B": 24},
        seg_extra={"A": {"gap_item_ids": ["item_a", "item_b"]}, "B": {"gap_item_ids": []}},
    )
    assert _named_gap_ids(payload) == ["item_a", "item_b"]


# ---------- P2-1: 未找到同触发封顶 ----------

def test_notfound_triggers_cap_89():
    """只有 G 段一条「未找到」（非需关注）也必须封顶 89，不得 90+（P2-1）."""
    items = [
        {"id": "g1", "name": "签署与印章", "segment": "G", "status": "未找到", "category_na": False},
    ]
    segments = scorecard.load_scorecard_config("procurement")["segments"]
    payload = _model_payload(100, {s["key"]: s["weight"] for s in segments}, summary="良好。")
    final = scorecard.postprocess(payload, items, segments)
    assert final["total"] <= 89, f"未找到未封顶，实际 {final['total']}"
    assert final["caps_applied"], "未找到触发封顶必须留消息"
    assert "签署与印章" in final["caps_applied"][0]
    # 扣分下限同时生效：G 段 10 权重 × 60% → ≤4
    by_key = {s["key"]: s for s in final["segments"]}
    assert by_key["G"]["score"] <= 4


def test_notfound_in_core_segment_triggers_cap_74():
    """核心段 B 出「未找到」→ 封顶 74（未找到不比重度更轻）."""
    items = [
        {"id": "b1", "name": "标的", "segment": "B", "status": "未找到", "category_na": False},
    ]
    segments = scorecard.load_scorecard_config("procurement")["segments"]
    payload = _model_payload(100, {s["key"]: s["weight"] for s in segments}, summary="良好。")
    final = scorecard.postprocess(payload, items, segments)
    assert final["total"] <= 74, f"核心段未找到未封顶 74，实际 {final['total']}"


def test_notfound_tier_never_says_no_risk_wording():
    """90+ 档引导语不得出现盖章/省略人工动作的措辞（P2-1 文案侧）."""
    hint = scorecard.tier_of(95)["hint"]
    for banned in ("按你的流程走就行", "可以放心签署", "没问题"):
        assert banned not in hint


# ---------- P2-2: 封顶消息列全部硬伤 ----------

def test_cap74_message_lists_noncore_hard_items_too():
    """cap=74 时若 A 段也有需关注，消息不能只列 B/D 段条款（P2-2）."""
    items = [
        {"id": "sm", "name": "标的", "segment": "B", "status": "需关注", "category_na": False},
        {"id": "a1", "name": "主体", "segment": "A", "status": "需关注", "category_na": False},
    ]
    segments = scorecard.load_scorecard_config("procurement")["segments"]
    payload = _model_payload(
        100, {s["key"]: s["weight"] for s in segments},
        comments={"A": "主体已说明。", "B": "标的已说明。"},
    )
    final = scorecard.postprocess(payload, items, segments)
    assert final["total"] == 74
    msg = final["caps_applied"][0]
    assert "【标的】" in msg and "【主体】" in msg, f"封顶消息须点名全部硬伤：{msg}"


# ---------- P3-4: pipeline 短路 reason ----------

def test_pipeline_error_reason_is_error_not_no_rule_results():
    out = node_model_review({"error": "解析失败"})
    assert out["scorecard"]["available"] is False
    assert out["scorecard"]["reason"] == "error"
