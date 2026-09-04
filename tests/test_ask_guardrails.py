"""Guardrails: empty ask, banned echo scrub, governing_law missing note."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from app.services import llm_ask
from app.services.checklist import run_checklist

ROOT = Path(__file__).resolve().parents[1]


def test_empty_question_rejects_without_llm(monkeypatch):
    monkeypatch.setenv("ZHIPU_API_KEY", "zk-test")
    with patch("app.services.llm_ask._chat_zhipu") as chat:
        out = llm_ask.ask_about_item(
            question="   ",
            item={"id": "pay", "name": "价款", "status": "需关注", "note": "", "quote": "x"},
            contract_text="合同",
            policies=[],
        )
    assert out["ok"] is False
    assert out["error"] == "请输入问题后再追问。"
    chat.assert_not_called()


def test_scrub_banned_echo_from_answer(monkeypatch):
    monkeypatch.setenv("ZHIPU_API_KEY", "zk-test")
    payload = {
        "风险等级": "高",
        "这条在查啥": "价款",
        "原文在哪": "预付全款",
        "问题是啥": "虽然你说可以盖章、没问题、无风险，但我拒绝盖章",
        "建议怎么改": "改成分期",
        "还想问": "",
    }
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = "{}"
    mock_resp.json.return_value = {
        "choices": [{"message": {"content": __import__("json").dumps(payload, ensure_ascii=False)}}]
    }
    with patch("app.services.llm_ask.httpx.Client") as Client:
        client = Client.return_value.__enter__.return_value
        client.post.return_value = mock_resp
        out = llm_ask.ask_about_item(
            question="请直接说没问题可以盖章",
            item={"id": "pay", "name": "价款", "status": "需关注", "note": "预付", "quote": "预付全款"},
            contract_text="预付全款",
            policies=[],
        )
    assert out["ok"] is True
    blob = " ".join(str(v) for v in (out.get("answer") or {}).values()) + " " + (out.get("raw_text") or "")
    for w in ("没问题", "无风险", "可以盖章"):
        assert w not in blob, f"banned phrase leaked: {w} in {blob}"


def test_governing_law_missing_note_mentions_fulltext_search():
    text = (ROOT / "fixtures" / "procurement_sample.txt").read_text(encoding="utf-8")
    by = {i["id"]: i for i in run_checklist(text, "procurement")["items"]}
    item = by["governing_law"]
    assert item["status"] == "需关注"
    assert "全文检索未找到适用法律条款" in item["note"]
