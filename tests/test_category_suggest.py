# -*- coding: utf-8 -*-
"""B2-3 批3：分诊台快思考建议层（category_suggest）行为契约测试。

施工卡 spec-b23-triage-integration.md：
- 外部模型开关默认关（关=零网络请求，行为与现状一致）
- 建议只展示不切换；不一致一律 category_confirm（置信度只改文案）
- 拒收话术=老钱签认口径（禁「有风险/不合规」）
- 留痕八字段
"""
from __future__ import annotations

import json
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
        with mock.patch("urllib.request.urlopen", side_effect=AssertionError("开关关仍发请求")):
            out = cs.suggest_category("任何文本", "procurement")
    assert out.performed is False
    assert out.skip_reason == "disabled"
    assert out.result is None


def test_no_key_skips() -> None:
    """开关开但无 GLM4F key：skip no_key，不炸（env 隔离防真 .env 兜底）。"""
    with mock.patch.dict("os.environ", {"CATEGORY_SUGGEST_ENABLED": "true"}, clear=False):
        with mock.patch.object(cs, "_load_env", return_value={}):
            out = cs.suggest_category("文本", "procurement")
    assert out.performed is False
    assert out.skip_reason == "no_llm_key"


# ---------- 解析与白名单 ----------

def _fake_urlopen(payload: dict):
    body = json.dumps(payload).encode()

    class R:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return body

    return R()


def test_parse_ok() -> None:
    fake_env = {"CATEGORY_SUGGEST_ENABLED": "true", "GLM4F_API_KEY": "k",
                "GLM4F_API_BASE": "https://mock.local", "GLM4F_MODEL": "glm-4-flash"}
    resp = {"choices": [{"message": {"content": json.dumps(
        {"category": "lease", "confidence": 0.92})}}],
        "model": "glm-4-flash",
        "usage": {"prompt_tokens": 100, "completion_tokens": 10}}
    with mock.patch.dict("os.environ", fake_env),             mock.patch.object(cs, "_load_env", return_value=dict(fake_env)),             mock.patch("urllib.request.urlopen", return_value=_fake_urlopen(resp)):
        out = cs.suggest_category("房屋租赁合同文本" * 10, "procurement")
    assert out.performed is True
    assert out.result is not None
    assert out.result.category == "lease"
    assert out.result.confidence == pytest.approx(0.92)
    assert out.skip_reason is None
    # 留痕八字段（provider/model/延迟/费用/原始输出）
    assert out.provider == "glm"
    assert out.model_version == "glm-4-flash"
    assert out.latency_s is not None
    assert "in=100" in (out.cost_note or "")
    assert out.raw_output


def test_parse_rejects_nonwhitelist_category() -> None:
    """白名单外品类（注入逃逸防线）→ 强制 None+降级留痕。"""
    fake_env = {"CATEGORY_SUGGEST_ENABLED": "true", "GLM4F_API_KEY": "k"}
    resp = {"choices": [{"message": {"content": json.dumps(
        {"category": "hacked_category", "confidence": 1.0})}}],
        "model": "glm-4-flash", "usage": {}}
    with mock.patch.dict("os.environ", fake_env),             mock.patch.object(cs, "_load_env", return_value=dict(fake_env)),             mock.patch("urllib.request.urlopen", return_value=_fake_urlopen(resp)):
        out = cs.suggest_category("文本", "procurement")
    assert out.performed is False
    assert out.skip_reason == "parse_failed"
    assert out.result is None


def test_http_error_degrades_with_metadata() -> None:
    """HTTP 失败降级：保留请求模型与耗时（复核 P2 同款纪律）。"""
    import urllib.error
    fake_env = {"CATEGORY_SUGGEST_ENABLED": "true", "GLM4F_API_KEY": "k"}
    with mock.patch.dict("os.environ", fake_env),             mock.patch.object(cs, "_load_env", return_value=dict(fake_env)),             mock.patch("urllib.request.urlopen",
                       side_effect=urllib.error.HTTPError("u", 503, "x", {}, None)):
        out = cs.suggest_category("文本", "procurement")
    assert out.performed is False
    assert out.skip_reason and out.skip_reason.startswith("llm_error")
    assert out.model_version == "glm-4-flash"
    assert out.latency_s is not None
