# -*- coding: utf-8 -*-
"""B2-3 批3：分诊台快思考建议层（category_suggest）行为契约测试。

施工卡 spec-b23-triage-integration.md：
- 外部模型开关默认关（关=零网络请求，行为与现状一致）
- 建议只展示不切换；不一致一律 category_confirm（置信度只改文案）
- 拒收话术=老钱签认口径（禁「有风险/不合规」）
- 留痕八字段
"""
from __future__ import annotations

from unittest import mock

import pytest

from app.services import category_suggest as cs


# ---------- 开关（默认关） ----------

def test_switch_default_off() -> None:
    """外部模型开关默认关：不设 env 时 is_enabled()=False。"""
    with mock.patch.dict("os.environ", {}, clear=False):
        import os
        os.environ.pop("CATEGORY_SUGGEST_ENABLED", None)
        assert cs.is_enabled() is False


def test_switch_on() -> None:
    with mock.patch.dict("os.environ", {"CATEGORY_SUGGEST_ENABLED": "true"}):
        assert cs.is_enabled() is True


def test_disabled_means_no_network() -> None:
    """开关关=零网络请求（施工卡红线）：urllib 一旦被碰就炸。"""
    with mock.patch.dict("os.environ", {"CATEGORY_SUGGEST_ENABLED": "false"}):
        with mock.patch("app.services.llm_client.httpx.Client",
                        side_effect=AssertionError("开关关仍发请求")):
            out = cs.suggest_category("任何文本", "procurement")
    assert out.performed is False
    assert out.skip_reason == "disabled"
    assert out.result is None


def test_no_key_skips() -> None:
    """开关开但无 CF_API_TOKEN：skip no_key，不炸（env 隔离防真 .env 兜底）。"""
    with mock.patch.dict("os.environ", {"CATEGORY_SUGGEST_ENABLED": "true"}, clear=False):
        with mock.patch.object(cs, "_load_env", return_value={}):
            out = cs.suggest_category("文本", "procurement")
    assert out.performed is False
    assert out.skip_reason == "no_llm_key"


# ---------- 解析与白名单 ----------

def test_parse_ok() -> None:
    fake_env = {"CATEGORY_SUGGEST_ENABLED": "true", "CF_API_TOKEN": "k",
        "CF_ACCOUNT_ID": "acct", "CF_CLEF_MODEL": "clef-flash"}
    with mock.patch.dict("os.environ", fake_env),             mock.patch.object(cs, "_load_env", return_value=dict(fake_env)),             mock.patch("app.services.llm_client.httpx.Client") as fake_client:
        fake_client.return_value.__enter__ = lambda s: s
        fake_client.return_value.__exit__ = lambda s, *a: False
        fake_client.return_value.post.return_value.json.return_value = {
            "success": True,
            "result": {"model": "clef-flash",
                       "answers": {"category": {"type": "choice", "choice": "lease",
                                                "probabilities": {"lease": 0.92},
                                                "confidence": 0.92}},
                       "usage": {"input_tokens": 100, "output_tokens": 0}},
        }
        fake_client.return_value.post.return_value.status_code = 200
        out = cs.suggest_category("房屋租赁合同文本" * 10, "procurement")
    assert out.performed is True
    assert out.result is not None
    assert out.result.category == "lease"
    assert out.result.confidence == pytest.approx(0.92)
    assert out.skip_reason is None
    # 留痕八字段（provider/model/延迟/费用/原始输出）
    assert out.provider == "clef"
    assert out.model_version == "clef-flash"
    assert out.latency_s is not None
    assert "账本" in (out.cost_note or "")
    assert out.raw_output


def test_parse_rejects_nonwhitelist_category() -> None:
    """白名单外品类（注入逃逸防线）→ 强制 None+降级留痕。"""
    fake_env = {"CATEGORY_SUGGEST_ENABLED": "true", "CF_API_TOKEN": "k",
        "CF_ACCOUNT_ID": "acct", "CF_CLEF_MODEL": "clef-flash"}
    with mock.patch.dict("os.environ", fake_env),             mock.patch.object(cs, "_load_env", return_value=dict(fake_env)),             mock.patch("app.services.llm_client.httpx.Client") as fake_client:
        fake_client.return_value.__enter__ = lambda s: s
        fake_client.return_value.__exit__ = lambda s, *a: False
        fake_client.return_value.post.return_value.json.return_value = {
            "success": True,
            "result": {"answers": {"category": {"type": "choice",
                                                "choice": "hacked_category",
                                                "confidence": 1.0}},
                       "usage": {}},
        }
        fake_client.return_value.post.return_value.status_code = 200
        out = cs.suggest_category("文本", "procurement")
    assert out.performed is False
    assert out.skip_reason == "parse_failed"
    assert out.result is None


def test_llm_call_record_emitted() -> None:
    """P1-1（肉饼门禁）：调用必须经 transport 单点落账本
    （LLMCallRecord node=category_suggest，provider=typesafe）。"""
    from app.services import llm_call_log

    fake_env = {"CATEGORY_SUGGEST_ENABLED": "true", "CF_API_TOKEN": "k",
        "CF_ACCOUNT_ID": "acct", "CF_CLEF_MODEL": "clef-flash"}
    with mock.patch.dict("os.environ", fake_env),             mock.patch.object(cs, "_load_env", return_value=dict(fake_env)),             mock.patch("app.services.llm_client.httpx.Client") as fake_client,             mock.patch.object(llm_call_log, "emit") as fake_emit:
        fake_client.return_value.__enter__ = lambda s: s
        fake_client.return_value.__exit__ = lambda s, *a: False
        fake_client.return_value.post.return_value.json.return_value = {
            "success": True,
            "result": {"answers": {"category": {"type": "choice", "choice": "nda",
                                                "confidence": 1.0}},
                       "usage": {"input_tokens": 50, "output_tokens": 0}},
        }
        fake_client.return_value.post.return_value.status_code = 200
        with llm_call_log.record_node("category_suggest",
                                      prompt_version=cs.PROMPT_VERSION):
            out = cs.suggest_category("保密协议文本", "procurement")
    assert out.performed is True
    assert fake_emit.called, "transport 未落账本（P1-1 复发）"
    rec = fake_emit.call_args[0][0]
    assert rec.node == "category_suggest"
    assert rec.provider == "cloudflare"
    assert rec.usage.get("input_tokens") == 50


def test_http_error_degrades_with_metadata() -> None:
    """HTTP 失败降级：保留请求模型与耗时（复核 P2 同款纪律）。"""
    import httpx
    fake_env = {"CATEGORY_SUGGEST_ENABLED": "true", "CF_API_TOKEN": "k",
        "CF_ACCOUNT_ID": "acct", "CF_CLEF_MODEL": "clef-flash"}
    with mock.patch.dict("os.environ", fake_env),             mock.patch.object(cs, "_load_env", return_value=dict(fake_env)),             mock.patch("app.services.llm_client.httpx.Client") as fake_client:
        fake_client.return_value.__enter__ = lambda s: s
        fake_client.return_value.__exit__ = lambda s, *a: False
        fake_client.return_value.post.side_effect = httpx.ConnectError("down")
        out = cs.suggest_category("文本", "procurement")
    assert out.performed is False
    assert out.skip_reason and out.skip_reason.startswith("llm_error")
    assert out.model_version == "clef-flash"
    assert out.latency_s is not None


def test_default_model_is_clef_flash(monkeypatch) -> None:
    """复核 P1-1：未配 CF_CLEF_MODEL 时请求的也是 clef-flash（防静默升级 27B 贵档）。"""
    fake_env = {"CATEGORY_SUGGEST_ENABLED": "true", "CF_API_TOKEN": "k",
                "CF_ACCOUNT_ID": "acct"}  # 故意不设 CF_CLEF_MODEL
    captured = {}

    def fake_post(url, **kw):
        captured["url"] = url
        captured["json"] = kw.get("json") or {}
        r = mock.Mock(status_code=200)
        r.json.return_value = {"success": True, "result": {
            "answers": {"category": {"type": "choice", "choice": "lease",
                                     "confidence": 0.9}},
            "usage": {"input_tokens": 1, "output_tokens": 0}}}
        return r

    with mock.patch.dict("os.environ", fake_env), \
            mock.patch.object(cs, "_load_env", return_value=dict(fake_env)), \
            mock.patch("app.services.llm_client.httpx.Client") as fake_client:
        fake_client.return_value.__enter__ = lambda s: s
        fake_client.return_value.__exit__ = lambda s, *a: False
        fake_client.return_value.post = mock.Mock(side_effect=fake_post)
        out = cs.suggest_category("租赁文本", "procurement")
    assert out.performed is True
    assert out.model_version == "clef-flash"
    assert "/clef-flash" in captured["url"], captured["url"]


def test_missing_account_id_degrades(monkeypatch) -> None:
    """复核 P1-1：缺 CF_ACCOUNT_ID 明确降级（no_account_id），不拼空账户 URL。"""
    fake_env = {"CATEGORY_SUGGEST_ENABLED": "true", "CF_API_TOKEN": "k"}
    with mock.patch.dict("os.environ", fake_env), \
            mock.patch.object(cs, "_load_env", return_value=dict(fake_env)), \
            mock.patch("app.services.llm_client.httpx.Client",
                       side_effect=AssertionError("缺账户仍发请求")):
        out = cs.suggest_category("文本", "procurement")
    assert out.performed is False
    assert out.skip_reason == "no_account_id"


def test_transient_error_retries_once(monkeypatch) -> None:
    """复核 P1-2：瞬态网络错误有限重试一次——首试断连、重试成功。"""
    import httpx
    fake_env = {"CATEGORY_SUGGEST_ENABLED": "true", "CF_API_TOKEN": "k",
                "CF_ACCOUNT_ID": "acct", "CF_CLEF_MODEL": "clef-flash"}
    calls = []

    def flaky_post(url, **kw):
        calls.append(1)
        if len(calls) == 1:
            raise httpx.RemoteProtocolError("first try drops")
        r = mock.Mock(status_code=200)
        r.json.return_value = {"success": True, "result": {
            "answers": {"category": {"type": "choice", "choice": "lease",
                                     "confidence": 0.9}},
            "usage": {"input_tokens": 1, "output_tokens": 0}}}
        return r

    with mock.patch.dict("os.environ", fake_env), \
            mock.patch.object(cs, "_load_env", return_value=dict(fake_env)), \
            mock.patch("app.services.llm_client.httpx.Client") as fake_client:
        fake_client.return_value.__enter__ = lambda s: s
        fake_client.return_value.__exit__ = lambda s, *a: False
        fake_client.return_value.post = mock.Mock(side_effect=flaky_post)
        out = cs.suggest_category("租赁文本", "procurement")
    assert len(calls) == 2, "应重试一次"
    assert out.performed is True and out.attempt == 2


def test_permanent_rejection_not_retried(monkeypatch) -> None:
    """ChatGPT 复核 P1：451/401/400 等永久性拒绝只请求一次（不重试）。"""
    import httpx
    fake_env = {"CATEGORY_SUGGEST_ENABLED": "true", "CF_API_TOKEN": "k",
                "CF_ACCOUNT_ID": "acct", "CF_CLEF_MODEL": "clef-flash"}
    calls = []

    def rejecting_post(url, **kw):
        calls.append(1)
        r = mock.Mock(status_code=451)
        r.request = mock.Mock()
        raise httpx.HTTPStatusError("451 region", request=r.request, response=r)

    with mock.patch.dict("os.environ", fake_env), \
            mock.patch.object(cs, "_load_env", return_value=dict(fake_env)), \
            mock.patch("app.services.llm_client.httpx.Client") as fake_client:
        fake_client.return_value.__enter__ = lambda s: s
        fake_client.return_value.__exit__ = lambda s, *a: False
        fake_client.return_value.post = mock.Mock(side_effect=rejecting_post)
        out = cs.suggest_category("文本", "procurement")
    assert len(calls) == 1, f"永久拒绝被重试了 {len(calls)} 次"
    assert out.performed is False
    assert out.attempt == 1


def test_retry_records_attempts_1_and_2_in_ledger(monkeypatch) -> None:
    """ChatGPT 复核：重试的两笔调用在账本里 attempt 分别为 1、2。"""
    import httpx
    from app.services import llm_call_log
    fake_env = {"CATEGORY_SUGGEST_ENABLED": "true", "CF_API_TOKEN": "k",
                "CF_ACCOUNT_ID": "acct", "CF_CLEF_MODEL": "clef-flash"}
    calls = []

    def flaky_post(url, **kw):
        calls.append(kw.get("json", {}).get("_attempt_marker") or len(calls))
        if len(calls) == 1:
            raise httpx.RemoteProtocolError("first try drops")
        r = mock.Mock(status_code=200)
        r.json.return_value = {"success": True, "result": {
            "answers": {"category": {"type": "choice", "choice": "lease",
                                     "confidence": 0.9}},
            "usage": {"input_tokens": 1, "output_tokens": 0}}}
        return r

    with mock.patch.dict("os.environ", fake_env), \
            mock.patch.object(cs, "_load_env", return_value=dict(fake_env)), \
            mock.patch("app.services.llm_client.httpx.Client") as fake_client, \
            mock.patch.object(llm_call_log, "emit") as fake_emit:
        fake_client.return_value.__enter__ = lambda s: s
        fake_client.return_value.__exit__ = lambda s, *a: False
        fake_client.return_value.post = mock.Mock(side_effect=flaky_post)
        out = cs.suggest_category("租赁文本", "procurement")
    assert out.performed is True and out.attempt == 2
    attempts = [rec.attempt for rec in fake_emit.call_args_list
                for rec in [rec.args[0]]]
    assert attempts == [1, 2], f"账本 attempt 序列错误：{attempts}"
