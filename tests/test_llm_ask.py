"""Unit tests for llm_ask (no real API key required)."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from app.services import llm_ask


def test_ask_without_any_key_returns_not_enabled(monkeypatch):
    for k in ("ZHIPU_API_KEY", "GLM_API_KEY", "XAI_API_KEY", "GROK_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    result = llm_ask.ask_about_item(
        question="风险大吗？",
        item={"id": "pay", "name": "价款", "status": "需关注", "note": "", "quote": "…"},
        contract_text="合同全文",
        policies=["先验收后付款"],
    )
    assert result["ok"] is False
    assert result["error"] == "追问暂未开通"


def test_get_api_key_true_if_zhipu_present(monkeypatch):
    for k in ("ZHIPU_API_KEY", "GLM_API_KEY", "XAI_API_KEY", "GROK_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    assert llm_ask.get_api_key() is None
    monkeypatch.setenv("ZHIPU_API_KEY", "zk-test")
    assert llm_ask.get_api_key() == "zk-test"


def test_zhipu_http_mocked(monkeypatch):
    for k in ("ZHIPU_API_KEY", "GLM_API_KEY", "XAI_API_KEY", "GROK_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("ZHIPU_API_KEY", "zk-test")
    monkeypatch.setenv("GLM_MODEL", "glm-5.2")

    payload = {
        "风险等级": "高",
        "这条在查啥": "价款",
        "原文在哪": "「预付全款」",
        "问题是啥": "预付过高",
        "建议怎么改": "改为验收后付",
        "还想问": "质保金比例？",
    }

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = "{}"
    mock_resp.json.return_value = {
        "choices": [{"message": {"content": json.dumps(payload, ensure_ascii=False)}}]
    }

    with patch("app.services.llm_ask.httpx.Client") as Client:
        client = Client.return_value.__enter__.return_value
        client.post.return_value = mock_resp
        result = llm_ask.ask_about_item(
            question="风险大吗？",
            item={
                "id": "pay",
                "name": "价款与支付",
                "status": "需关注",
                "note": "预付",
                "quote": "预付全款",
            },
            contract_text="甲方预付全款后乙方发货。",
            policies=["先验收后付款"],
        )

    assert result["ok"] is True
    assert result["answer"]["风险等级"] == "高"
    assert "预付" in result["answer"]["原文在哪"] or "预付" in result["answer"]["问题是啥"]
    call_kwargs = client.post.call_args
    assert call_kwargs.args[0] == llm_ask._zhipu_chat_url()
    body = call_kwargs.kwargs["json"]
    assert body["model"] == "glm-5.2"
    assert abs(body["temperature"] - 0.3) < 1e-6
    assert call_kwargs.kwargs["headers"]["Authorization"] == "Bearer zk-test"


def test_prefers_zhipu_over_xai(monkeypatch):
    monkeypatch.setenv("ZHIPU_API_KEY", "zk")
    monkeypatch.setenv("XAI_API_KEY", "xk")
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = "{}"
    mock_resp.json.return_value = {
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {k: "x" for k in llm_ask.OUTPUT_FIELDS}, ensure_ascii=False
                    )
                }
            }
        ]
    }
    with patch("app.services.llm_ask.httpx.Client") as Client:
        client = Client.return_value.__enter__.return_value
        client.post.return_value = mock_resp
        llm_ask.ask_about_item(
            question="?",
            item={"id": "a", "name": "a", "status": "需关注", "note": "", "quote": "q"},
            contract_text="t",
            policies=[],
        )
    assert client.post.call_args.args[0] == llm_ask._zhipu_chat_url()


def test_rewrite_field_passes_through(monkeypatch):
    """M3.5 改写稿：模型返回新字段时透传给前端."""
    monkeypatch.setenv("ZHIPU_API_KEY", "zk")
    payload = {
        "风险等级": "中",
        "这条在查啥": "付款",
        "原文在哪": "「签约即付全款」",
        "问题是啥": "付款节奏失衡",
        "建议怎么改": "改为验收后付款",
        "改写稿": "货物经甲方验收合格后 10 个工作日内，甲方向乙方支付合同总价款的 90%；其余 10% 作为质保金，质保期满后支付。",
        "还想问": "质保期多久？",
    }
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = "{}"
    mock_resp.json.return_value = {
        "choices": [{"message": {"content": json.dumps(payload, ensure_ascii=False)}}]
    }
    with patch("app.services.llm_ask.httpx.Client") as Client:
        client = Client.return_value.__enter__.return_value
        client.post.return_value = mock_resp
        result = llm_ask.ask_about_item(
            question="怎么改？",
            item={"id": "pay", "name": "价款", "status": "需关注", "note": "", "quote": "签约即付全款"},
            contract_text="签约即付全款。",
            policies=[],
        )
    assert result["ok"] is True
    assert "质保金" in result["answer"]["改写稿"]


def test_rewrite_banned_stamp_scrubbed(monkeypatch):
    """改写稿不得宣称「已无风险/已合规」——命中必须过滤."""
    monkeypatch.setenv("ZHIPU_API_KEY", "zk")
    payload = {
        "风险等级": "中",
        "这条在查啥": "付款",
        "原文在哪": "「签约即付全款」",
        "问题是啥": "付款节奏失衡",
        "建议怎么改": "改为验收后付款",
        "改写稿": "验收后付款。已无风险，已合规。",
        "还想问": "",
    }
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = "{}"
    mock_resp.json.return_value = {
        "choices": [{"message": {"content": json.dumps(payload, ensure_ascii=False)}}]
    }
    with patch("app.services.llm_ask.httpx.Client") as Client:
        client = Client.return_value.__enter__.return_value
        client.post.return_value = mock_resp
        result = llm_ask.ask_about_item(
            question="怎么改？",
            item={"id": "pay", "name": "价款", "status": "需关注", "note": "", "quote": "签约即付全款"},
            contract_text="签约即付全款。",
            policies=[],
        )
    rewrite = result["answer"]["改写稿"]
    assert "已无风险" not in rewrite
    assert "已合规" not in rewrite
