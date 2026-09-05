"""Deterministic tests for M3.5 scorecard (mocked LLM, no key needed).

Covers 老王 gate matrix + 老钱 opinion书 enforcement:
- 有规则结果才出分；关补盲仍有分；无 Key 明确未开通
- caps/floors enforced in code, not model trust
- forbidden hit → retry once → degrade to score-only
- naming incomplete → degrade
"""
from __future__ import annotations

import json
from pathlib import Path

from app.services import scorecard
from app.services.blind_spot import annotate_rule_items
from app.services.checklist import run_checklist
from app.services.model_review import run_model_review

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "fixtures" / "procurement_sample.txt"


def _rule_items():
    text = FIXTURE.read_text(encoding="utf-8")
    result = run_checklist(text, category="procurement")
    return text, annotate_rule_items(result["items"])


def _payload(raw_dict):
    return json.dumps(raw_dict, ensure_ascii=False)


def _model_payload(total, seg_scores, summary="汇总。", comments=None, candidates=None):
    segs = []
    for key, sc in seg_scores.items():
        c = {"key": key, "score": sc}
        if comments and key in comments:
            c["comment"] = comments[key]
        segs.append(c)
    return {
        "scorecard": {"summary": summary, "segments": segs},
        "candidates": candidates or [],
    }


# ---------- config ----------

def test_scorecard_config_loads_and_weights_sum_100():
    for category in ("procurement", "nda"):
        cfg = scorecard.load_scorecard_config(category)
        segs = cfg["segments"]
        assert len(segs) == 7
        assert {s["key"] for s in segs} == {"A", "B", "C", "D", "E", "F", "G"}
        assert sum(s["weight"] for s in segs) == 100
    nda = {s["key"]: s for s in scorecard.load_scorecard_config("nda")["segments"]}
    assert nda["C"]["na"] is True and nda["C"]["weight"] == 0


def test_forbidden_list_loads():
    fb = scorecard.load_forbidden()
    assert fb, "forbidden config must not be empty"
    all_words = [w for words in fb.values() for w in words]
    assert "本合同没有问题" in all_words
    assert "可以放心签署" in all_words


def test_check_and_scrub_forbidden():
    hits = scorecard.check_forbidden("总体看本合同没有问题，可以放心签署。")
    assert "本合同没有问题" in hits
    scrubbed = scorecard.scrub_forbidden("本合同没有问题")
    assert "本合同没有问题" not in scrubbed


# ---------- postprocess: floors & caps (code-enforced, 老钱第三节) ----------

def test_cap_core_attention_limits_total():
    _, items = _rule_items()
    segments = scorecard.load_scorecard_config("procurement")["segments"]
    # model tries to give a shiny 95 despite B/D 需关注 items in gold fixture
    payload = _model_payload(
        95, {s["key"]: s["weight"] for s in segments},
        summary="整体良好。", comments={"B": "需关注项已说明：标的。", "D": "需关注项已说明：违约责任。"},
    )
    final = scorecard.postprocess(payload, items, segments)
    assert final["total"] <= 74
    # floors pulled B/D segment scores below weight
    by_key = {s["key"]: s for s in final["segments"]}
    assert by_key["B"]["score"] < by_key["B"]["weight"]
    assert by_key["D"]["score"] < by_key["D"]["weight"]


def test_cap_applied_message_when_floors_not_enough():
    """非核心段需关注：下限压不完，封顶 89 必须实际生效并留消息."""
    items = [
        {"id": "a", "name": "主体", "segment": "A", "status": "需关注", "category_na": False},
        {"id": "b", "name": "标的", "segment": "B", "status": "通过", "category_na": False},
    ]
    segments = scorecard.load_scorecard_config("procurement")["segments"]
    payload = _model_payload(
        100, {s["key"]: s["weight"] for s in segments},
        summary="整体良好。", comments={"A": "需关注项已说明：主体。"},
    )
    final = scorecard.postprocess(payload, items, segments)
    assert final["total"] <= 89
    assert final["caps_applied"], "cap 89 should actually truncate and leave a message"


def test_total_recomputed_from_segments_not_model():
    _, items = _rule_items()
    segments = scorecard.load_scorecard_config("procurement")["segments"]
    payload = _model_payload(
        95, {s["key"]: s["weight"] for s in segments}, summary="无问题也不信模型总分"
    )
    final = scorecard.postprocess(payload, items, segments)
    assert final["total"] == sum(s["score"] for s in final["segments"])
    assert final["total"] < 95  # gold has 需关注 items → cannot reach model claim


def test_na_segment_zero_and_disclaimer_present():
    text = (
        ROOT / "fixtures" / "nda_public_template.txt"
    ).read_text(encoding="utf-8")
    result = run_checklist(text, category="nda")
    items = annotate_rule_items(result["items"])
    segments = scorecard.load_scorecard_config("nda")["segments"]
    payload = _model_payload(100, {s["key"]: s["weight"] for s in segments}, summary="范本。")
    final = scorecard.postprocess(payload, items, segments)
    c_seg = next(s for s in final["segments"] if s["key"] == "C")
    assert c_seg["na"] is True and c_seg["score"] == 0
    assert final["disclaimer"]
    assert final["advisory_only"] is True


def test_forbidden_scrubbed_in_comments_and_summary():
    _, items = _rule_items()
    segments = scorecard.load_scorecard_config("procurement")["segments"]
    payload = _model_payload(
        80,
        {s["key"]: s["weight"] for s in segments},
        summary="本合同没有问题。",
    )
    final = scorecard.postprocess(payload, items, segments)
    assert "本合同没有问题" not in final["summary"]


# ---------- gates & degradation via run_model_review ----------

def _chat_returning(raw_dict, calls):
    def chat(_system, _user):
        calls.append(_user)
        return _payload(raw_dict)

    return chat


def test_gate_no_rule_results(monkeypatch):
    monkeypatch.setenv("BLIND_SPOT_ENABLED", "true")
    calls: list = []
    out = run_model_review(
        text="x", items=[], policies=[], category="procurement",
        chat_fn=_chat_returning(_model_payload(90, {}), calls),
    )
    assert out["scorecard"]["available"] is False
    assert out["scorecard"]["reason"] == "no_rule_results"
    assert not calls, "LLM must not be called without rule results"


def test_forbidden_retry_then_degrade(monkeypatch):
    monkeypatch.setenv("BLIND_SPOT_ENABLED", "true")
    text, items = _rule_items()
    segments = scorecard.load_scorecard_config("procurement")["segments"]
    bad = _model_payload(
        80, {s["key"]: s["weight"] for s in segments},
        summary="整体看本合同没有问题。",
    )
    calls: list = []
    out = run_model_review(
        text=text, items=items, policies=[], category="procurement",
        chat_fn=_chat_returning(bad, calls),
    )
    assert len(calls) == 2, "must retry exactly once on forbidden hit"
    assert out["scorecard"]["available"] is True
    assert out["scorecard"].get("degraded") is True
    assert "本合同没有问题" not in out["scorecard"]["summary"]


def test_naming_incomplete_degrades(monkeypatch):
    monkeypatch.setenv("BLIND_SPOT_ENABLED", "true")
    text, items = _rule_items()
    segments = scorecard.load_scorecard_config("procurement")["segments"]
    # comments never mention any 需关注 item names → degrade to score-only
    payload = _model_payload(80, {s["key"]: s["weight"] for s in segments}, summary="大致尚可。")
    calls: list = []
    out = run_model_review(
        text=text, items=items, policies=[], category="procurement",
        chat_fn=_chat_returning(payload, calls),
    )
    assert len(calls) == 1
    assert out["scorecard"].get("degraded") is True
    assert out["scorecard"]["summary"].startswith("规则结果汇总评分")


def test_clean_pass_not_degraded(monkeypatch):
    monkeypatch.setenv("BLIND_SPOT_ENABLED", "true")
    text, items = _rule_items()
    segments = scorecard.load_scorecard_config("procurement")["segments"]
    # mention every non-通过 item name in comments
    names = [i["name"] for i in items if i["status"] != "通过" and not i.get("category_na")]
    comments = {"A": "、".join(names)}
    payload = _model_payload(80, {s["key"]: s["weight"] for s in segments}, summary="点名：" + "、".join(names), comments=comments)
    out = run_model_review(
        text=text, items=items, policies=[], category="procurement",
        chat_fn=_chat_returning(payload, []),
    )
    assert out["scorecard"]["available"] is True
    assert out["scorecard"].get("degraded", False) is False


def test_scorecard_no_mutation_of_rule_items(monkeypatch):
    monkeypatch.setenv("BLIND_SPOT_ENABLED", "true")
    text, items = _rule_items()
    before = {i["id"]: i["status"] for i in items}
    segments = scorecard.load_scorecard_config("procurement")["segments"]
    names = [i["name"] for i in items if i["status"] != "通过" and not i.get("category_na")]
    payload = _model_payload(80, {s["key"]: s["weight"] for s in segments}, summary="点名：" + "、".join(names), comments={"A": "、".join(names)})
    run_model_review(text=text, items=items, policies=[], category="procurement", chat_fn=_chat_returning(payload, []))
    after = {i["id"]: i["status"] for i in items}
    assert before == after, "scorecard/blind must never mutate rule statuses"
