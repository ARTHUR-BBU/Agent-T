"""模型分级路由测试（阶段 0.5）。

核心红线：不设分级变量时发出的 HTTP payload 与历史版本一致（默认零行为
变化）。mock 模式对齐 test_llm_ask：patch llm_ask.httpx.Client 断言线路层。
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from app.services import llm_ask

# 三个供应商共用的 mock 响应构造
def _mock_resp(content: str = "{}") -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.text = content
    resp.json.return_value = {"choices": [{"message": {"content": content}}]}
    return resp


class _PostCapture:
    """patch httpx.Client 并捕获最近一次 POST 的 payload / timeout。"""

    def __init__(self):
        self.payload = None
        self.timeout = None
        self._patcher = patch("app.services.llm_ask.httpx.Client")
        self.Client = self._patcher.start()

    def __enter__(self):
        self.client = self.Client.return_value.__enter__.return_value
        self.client.post.return_value = _mock_resp()
        return self

    def read_last_call(self):
        call = self.client.post.call_args
        self.payload = call.kwargs["json"]
        self.timeout = self.Client.call_args.kwargs.get("timeout")
        return self

    def __exit__(self, *exc):
        self._patcher.stop()
        return False


# ---------- _model_for 解析矩阵 ----------

def test_model_for_falls_back_when_tier_unset(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_MODEL", raising=False)
    monkeypatch.delenv("DEEPSEEK_MODEL_PRECHECK", raising=False)
    monkeypatch.delenv("DEEPSEEK_MODEL_REVIEW", raising=False)
    assert llm_ask._model_for("DEEPSEEK_MODEL", "deepseek-v4-flash") == "deepseek-v4-flash"


def test_model_for_precheck_tier_overrides(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_MODEL", "base-model")
    monkeypatch.setenv("DEEPSEEK_MODEL_PRECHECK", "flash-model")
    assert llm_ask._model_for("DEEPSEEK_MODEL", "x", "precheck") == "flash-model"
    assert llm_ask._model_for("DEEPSEEK_MODEL", "x", "review") == "base-model", \
        "review 档不受 PRECHECK 变量影响"


def test_model_for_review_tier_overrides(monkeypatch):
    monkeypatch.setenv("GLM_MODEL", "base-glm")
    monkeypatch.setenv("GLM_MODEL_REVIEW", "heavy-glm")
    assert llm_ask._model_for("GLM_MODEL", "x", "review") == "heavy-glm"
    assert llm_ask._model_for("GLM_MODEL", "x", "precheck") == "base-glm"


def test_model_for_blank_tier_counts_as_unset(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_MODEL", "base-model")
    monkeypatch.setenv("DEEPSEEK_MODEL_PRECHECK", "   ")
    assert llm_ask._model_for("DEEPSEEK_MODEL", "x", "precheck") == "base-model"


# ---------- 线路层：payload.model 断言 ----------

def test_chat_deepseek_precheck_sends_tiered_model(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setenv("DEEPSEEK_MODEL", "base-ds")
    monkeypatch.setenv("DEEPSEEK_MODEL_PRECHECK", "flash-ds")
    with _PostCapture() as cap:
        llm_ask._chat_deepseek("sk-test", "sys", "usr", purpose="precheck")
    assert cap.read_last_call().payload["model"] == "flash-ds"


def test_chat_deepseek_default_payload_unchanged(monkeypatch):
    """回归红线：不传 purpose 且无分级变量 → payload 与历史版本逐字段一致。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.delenv("DEEPSEEK_MODEL_PRECHECK", raising=False)
    monkeypatch.delenv("DEEPSEEK_MODEL_REVIEW", raising=False)
    monkeypatch.delenv("DEEPSEEK_MODEL", raising=False)
    with _PostCapture() as cap:
        llm_ask._chat_deepseek("sk-test", "sys", "usr")
    cap.read_last_call()
    assert cap.payload["model"] == llm_ask.DEFAULT_DEEPSEEK_MODEL
    assert cap.payload["temperature"] == 0.3
    assert [m["role"] for m in cap.payload["messages"]] == ["system", "user"]


def test_chat_zhipu_review_tier(monkeypatch):
    monkeypatch.setenv("GLM_MODEL", "base-glm")
    monkeypatch.setenv("GLM_MODEL_REVIEW", "heavy-glm")
    with _PostCapture() as cap:
        llm_ask._chat_zhipu("zk", "sys", "usr", purpose="review")
    assert cap.read_last_call().payload["model"] == "heavy-glm"


def test_chat_xai_review_tier(monkeypatch):
    monkeypatch.setenv("GROK_MODEL", "base-grok")
    monkeypatch.setenv("GROK_MODEL_REVIEW", "heavy-grok")
    with _PostCapture() as cap:
        llm_ask._chat_xai("xk", "sys", "usr", purpose="review")
    assert cap.read_last_call().payload["model"] == "heavy-grok"


# ---------- xai 超时（原写死 60s 提为可配，默认不变） ----------

def test_chat_xai_timeout_default_60(monkeypatch):
    monkeypatch.delenv("XAI_TIMEOUT_SECONDS", raising=False)
    with _PostCapture() as cap:
        llm_ask._chat_xai("xk", "sys", "usr")
    cap.read_last_call()
    assert cap.timeout == 60.0, "默认值必须与历史写死值一致"


def test_chat_xai_timeout_env_override(monkeypatch):
    monkeypatch.setenv("XAI_TIMEOUT_SECONDS", "45")
    with _PostCapture() as cap:
        llm_ask._chat_xai("xk", "sys", "usr")
    assert cap.read_last_call().timeout == 45.0


def test_chat_xai_timeout_param_wins(monkeypatch):
    """预审路径传参覆盖（顺带修复：PRECHECK_TIMEOUT_SECONDS 此前对 xAI 失效）。"""
    monkeypatch.setenv("XAI_TIMEOUT_SECONDS", "45")
    with _PostCapture() as cap:
        llm_ask._chat_xai("xk", "sys", "usr", timeout=30.0, purpose="precheck")
    assert cap.read_last_call().timeout == 30.0


# ---------- ask / model_review 不传 purpose（默认 review 档，零改动） ----------

def test_ask_call_defaults_to_review_tier(monkeypatch):
    """ask_about_item 不感知 purpose：默认档随 DEEPSEEK_MODEL 走。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setenv("DEEPSEEK_MODEL", "base-ds")
    with _PostCapture() as cap:
        llm_ask.ask_about_item(
            question="风险大吗？",
            item={"id": "a", "name": "a", "status": "需关注", "note": "", "quote": "q"},
            contract_text="t",
            policies=[],
        )
    assert cap.read_last_call().payload["model"] == "base-ds"
