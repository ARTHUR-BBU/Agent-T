"""Deterministic unit tests for M2.5 blind-spot (mocked LLM)."""
from __future__ import annotations

import json
from pathlib import Path

from app.services import blind_spot
from app.services.checklist import run_checklist
from app.services.blind_spot import (
    SKIP_NO_QUOTE,
    annotate_rule_items,
    is_blind_spot_enabled,
    run_blind_spot_pass,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "fixtures" / "procurement_sample.txt"


def _rule_items():
    text = FIXTURE.read_text(encoding="utf-8")
    result = run_checklist(text, category="procurement")
    return text, annotate_rule_items(result["items"])


def test_enabled_default_true(monkeypatch):
    monkeypatch.delenv("BLIND_SPOT_ENABLED", raising=False)
    assert is_blind_spot_enabled() is True


def test_enabled_accepts_aliases(monkeypatch):
    for v in ("true", "TRUE", "1", "yes", "YES", "on"):
        monkeypatch.setenv("BLIND_SPOT_ENABLED", v)
        assert is_blind_spot_enabled() is True
    for v in ("false", "0", "no", "off", ""):
        monkeypatch.setenv("BLIND_SPOT_ENABLED", v)
        assert is_blind_spot_enabled() is False


def test_off_returns_empty_no_补盲_payload(monkeypatch):
    monkeypatch.setenv("BLIND_SPOT_ENABLED", "false")
    text, items = _rule_items()
    out = run_blind_spot_pass(text=text, items=items, policies=[])
    assert out["blind_enabled"] is False
    assert out["blind_candidates"] == []
    assert out["blind_skipped_reason"] is None
    blob = json.dumps(out, ensure_ascii=False)
    assert "补盲" not in blob


def test_no_key_skips_with_reason(monkeypatch):
    monkeypatch.setenv("BLIND_SPOT_ENABLED", "true")
    for k in ("ZHIPU_API_KEY", "GLM_API_KEY", "XAI_API_KEY", "GROK_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    text, items = _rule_items()
    out = run_blind_spot_pass(text=text, items=items, policies=[])
    assert out["blind_enabled"] is True
    assert out["blind_candidates"] == []
    assert out["blind_skipped_reason"] == "no_llm_key"


def test_mock_empty_array_no_candidates(monkeypatch):
    monkeypatch.setenv("BLIND_SPOT_ENABLED", "true")
    text, items = _rule_items()

    def chat(_system, _user):
        return "[]"

    out = run_blind_spot_pass(text=text, items=items, policies=[], chat_fn=chat)
    assert out["blind_candidates"] == []
    assert out["blind_skipped_reason"] is None


def test_mock_candidate_with_quote_for_gap(monkeypatch):
    monkeypatch.setenv("BLIND_SPOT_ENABLED", "true")
    text, items = _rule_items()
    # 管辖与争议 is 通过 on gold — a gap for blind
    gap = next(i for i in items if i["name"] == "管辖与争议")
    assert gap["status"] == "通过"
    # pick a contiguous snippet from text that mentions 管辖/诉讼/法院
    snippet = None
    for needle in ("管辖", "诉讼", "法院", "仲裁"):
        idx = text.find(needle)
        if idx >= 0:
            snippet = text[max(0, idx - 8) : idx + 20].replace("\n", " ").strip()
            break
    assert snippet, "fixture should contain jurisdiction wording"

    payload = [
        {
            "item_id": gap["id"],
            "name": gap["name"],
            "note": "管辖约定可能不利于采购方",
            "quote": snippet,
        }
    ]

    def chat(_system, _user):
        return json.dumps(payload, ensure_ascii=False)

    out = run_blind_spot_pass(text=text, items=items, policies=["测试政策"], chat_fn=chat)
    assert len(out["blind_candidates"]) == 1
    c = out["blind_candidates"][0]
    assert c["id"] == gap["id"]
    assert c["status"] == "需关注"
    assert c["tag_source"] == "blind"
    assert c["needs_confirm"] is True
    assert c["quote"] == snippet
    # rule statuses untouched
    by_id = {i["id"]: i for i in items}
    assert by_id[gap["id"]]["status"] == "通过"
    assert by_id[gap["id"]].get("tag_source") in (None, "rule")


def test_no_quote_skipped_message(monkeypatch):
    monkeypatch.setenv("BLIND_SPOT_ENABLED", "true")
    text, items = _rule_items()
    gap = next(i for i in items if i["status"] == "通过")

    def chat(_system, _user):
        return json.dumps(
            [{"item_id": gap["id"], "name": gap["name"], "note": "有风险", "quote": ""}],
            ensure_ascii=False,
        )

    out = run_blind_spot_pass(text=text, items=items, chat_fn=chat)
    assert out["blind_candidates"] == []
    assert any(SKIP_NO_QUOTE in m for m in out["blind_skipped_messages"])


def test_fake_quote_not_in_text_skipped(monkeypatch):
    monkeypatch.setenv("BLIND_SPOT_ENABLED", "true")
    text, items = _rule_items()
    gap = next(i for i in items if i["status"] == "通过")

    def chat(_system, _user):
        return json.dumps(
            [
                {
                    "item_id": gap["id"],
                    "name": gap["name"],
                    "note": "编造",
                    "quote": "这段话绝对不在合同里XYZ123",
                }
            ],
            ensure_ascii=False,
        )

    out = run_blind_spot_pass(text=text, items=items, chat_fn=chat)
    assert out["blind_candidates"] == []
    assert any(SKIP_NO_QUOTE in m for m in out["blind_skipped_messages"])


def test_never_overwrite_rule_attention(monkeypatch):
    monkeypatch.setenv("BLIND_SPOT_ENABLED", "true")
    text, items = _rule_items()
    att = next(i for i in items if i["status"] == "需关注")
    assert att.get("tag_source") == "rule"
    snippet = (att.get("quote") or text[:40]).strip() or text[10:40]

    def chat(_system, _user):
        return json.dumps(
            [
                {
                    "item_id": att["id"],
                    "name": att["name"],
                    "note": "试图覆盖",
                    "quote": snippet if snippet in text else text[20:50],
                }
            ],
            ensure_ascii=False,
        )

    out = run_blind_spot_pass(text=text, items=items, chat_fn=chat)
    # gaps filter excludes 需关注, so candidate must not appear
    assert all(c["id"] != att["id"] for c in out["blind_candidates"])
    # original still 需关注 / rule
    assert next(i for i in items if i["id"] == att["id"])["status"] == "需关注"


def test_annotate_rule_tag_source():
    items = [
        {"id": "a", "name": "A", "status": "需关注"},
        {"id": "b", "name": "B", "status": "通过"},
        {"id": "c", "name": "C", "status": "未找到"},
    ]
    out = annotate_rule_items(items)
    assert out[0]["tag_source"] == "rule"
    assert out[1]["tag_source"] is None
    assert out[2]["tag_source"] is None


def test_pipeline_blind_off_identical_to_rules(monkeypatch):
    monkeypatch.setenv("BLIND_SPOT_ENABLED", "false")
    for k in ("ZHIPU_API_KEY", "GLM_API_KEY", "XAI_API_KEY", "GROK_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    from app.graph.pipeline import reset_graph, run_review

    reset_graph()
    raw = FIXTURE.read_bytes()
    result = run_review("procurement_sample.txt", raw, category="procurement")
    assert result["error"] == ""
    assert result["blind_candidates"] == []
    assert result["blind_enabled"] is False
    attention = [i for i in result["items"] if i["status"] == "需关注"]
    assert len(attention) >= 7
    for i in attention:
        assert i.get("tag_source") == "rule"


def test_pipeline_mock_candidate(monkeypatch):
    monkeypatch.setenv("BLIND_SPOT_ENABLED", "true")
    for k in ("ZHIPU_API_KEY", "GLM_API_KEY", "XAI_API_KEY", "GROK_API_KEY"):
        monkeypatch.delenv(k, raising=False)

    text = FIXTURE.read_text(encoding="utf-8")
    items = annotate_rule_items(run_checklist(text, "procurement")["items"])
    gap = next(i for i in items if i["name"] == "管辖与争议")
    idx = text.find("管辖")
    snippet = text[max(0, idx - 4) : idx + 16].replace("\n", " ").strip()

    # Patch run_blind_spot_pass via chat by patching at pipeline import path
    from app.graph import pipeline
    from app.graph.pipeline import reset_graph

    reset_graph()

    real = blind_spot.run_blind_spot_pass

    def wrapped(**kwargs):
        return real(
            **kwargs,
            chat_fn=lambda _s, _u: json.dumps(
                [
                    {
                        "item_id": gap["id"],
                        "name": gap["name"],
                        "note": "候选风险",
                        "quote": snippet,
                    }
                ],
                ensure_ascii=False,
            ),
        )

    monkeypatch.setattr(pipeline, "run_blind_spot_pass", wrapped)
    reset_graph()
    result = pipeline.run_review("procurement_sample.txt", FIXTURE.read_bytes(), "procurement")
    assert len(result["blind_candidates"]) == 1
    assert result["blind_candidates"][0]["tag_source"] == "blind"
    # rule item status for 管辖 still 通过
    rule_j = next(i for i in result["items"] if i["name"] == "管辖与争议")
    assert rule_j["status"] == "通过"
