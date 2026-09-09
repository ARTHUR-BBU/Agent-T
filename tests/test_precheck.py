"""precheck 模块单测（mock chat_fn，不联网）。

对齐 docs/spec-llm-precheck.md 3.2/3.3 与小智娘测试计划第 1 部分：
Schema 校验、重试恰好一次、降级矩阵、白名单防线、禁语清洗、头尾采样。
"""
from __future__ import annotations

import json

import httpx
import pytest

from app.services import precheck
from app.services.precheck import PrecheckOutcome, PrecheckResult, decide_branch, run_precheck

GOOD = {
    "detected_type": "房屋租赁合同",
    "is_supported": True,
    "suggested_category": "lease",
    "confidence": "high",
    "summary": "以租金换取房屋使用权的合同",
}


@pytest.fixture(autouse=True)
def _precheck_on(monkeypatch):
    """模块级打开预审（conftest 默认关闭；同作用域 conftest 先应用，此处覆盖）。"""
    monkeypatch.setenv("PRECHECK_ENABLED", "true")


def _chat_returns(payload: str, calls: list):
    def chat(system: str, user: str) -> str:
        calls.append((system, user))
        return payload

    return chat


# ---------- 正常解析 ----------

def test_parse_bare_json_ok():
    calls: list = []
    outcome = run_precheck("合同正文", "lease", chat_fn=_chat_returns(json.dumps(GOOD), calls))
    assert outcome.performed and outcome.result is not None
    assert outcome.result.detected_type == "房屋租赁合同"
    assert outcome.result.suggested_category == "lease"
    assert outcome.result.confidence == "high"


def test_parse_fenced_json_ok():
    fenced = "```json\n" + json.dumps(GOOD, ensure_ascii=False) + "\n```"
    outcome = run_precheck("合同正文", "lease", chat_fn=_chat_returns(fenced, []))
    assert outcome.performed and outcome.result.suggested_category == "lease"


def test_missing_confidence_defaults_low_and_extra_fields_stripped():
    payload = {k: v for k, v in GOOD.items() if k != "confidence"} | {"notes": "多余字段"}
    outcome = run_precheck("合同正文", "lease", chat_fn=_chat_returns(json.dumps(payload), []))
    assert outcome.performed
    assert outcome.result.confidence == "low"
    assert not hasattr(outcome.result, "notes")


# ---------- 白名单防线（注入逃逸兜底） ----------

def test_suggested_outside_whitelist_forced_null_unsupported():
    payload = GOOD | {"suggested_category": "labor"}
    calls: list = []
    outcome = run_precheck("合同正文", "lease", chat_fn=_chat_returns(json.dumps(payload), calls))
    assert outcome.result.suggested_category is None
    assert outcome.result.is_supported is False
    assert len(calls) == 2, "自洽性矛盾（supported 却给不出合法建议）必须先重试一次"


def test_inconsistent_then_consistent_succeeds_on_retry():
    """P2-2（小智娘门禁）：首轮自洽性矛盾走重试，次轮正常则成功。"""
    calls: list = []

    def chat(system: str, user: str) -> str:
        calls.append((system, user))
        bad = GOOD | {"suggested_category": "labor"}
        return json.dumps(bad if len(calls) == 1 else GOOD, ensure_ascii=False)

    outcome = run_precheck("合同正文", "lease", chat_fn=chat)
    assert len(calls) == 2
    assert outcome.performed and outcome.result.is_supported is True
    assert outcome.result.suggested_category == "lease"


def test_supported_without_valid_suggestion_treated_unsupported():
    payload = GOOD | {"suggested_category": None}
    outcome = run_precheck("合同正文", "lease", chat_fn=_chat_returns(json.dumps(payload), []))
    assert outcome.result.is_supported is False


# ---------- 重试与降级矩阵（spec 3.5 逐行） ----------

def test_bad_json_twice_retries_exactly_once_then_skips():
    calls: list = []
    outcome = run_precheck("合同正文", "lease", chat_fn=_chat_returns("这不是JSON", calls))
    assert outcome.performed is False
    assert outcome.skip_reason == "parse_failed"
    assert len(calls) == 2, "必须恰好重试一次"


def test_bad_then_good_succeeds_on_second_call():
    calls: list = []

    def chat(system: str, user: str) -> str:
        calls.append((system, user))
        return "散文" if len(calls) == 1 else json.dumps(GOOD, ensure_ascii=False)

    outcome = run_precheck("合同正文", "lease", chat_fn=chat)
    assert len(calls) == 2
    assert outcome.performed and outcome.result.suggested_category == "lease"
    assert "再次提醒" in calls[1][0], "重试轮必须带加严提醒"


@pytest.mark.parametrize("exc", [
    httpx.TimeoutException("timeout"),
    httpx.HTTPStatusError("500", request=None, response=None),  # type: ignore[arg-type]
])
def test_call_exception_degrades_without_raising(exc):
    def chat(system: str, user: str) -> str:
        raise exc

    outcome = run_precheck("合同正文", "lease", chat_fn=chat)
    assert outcome.skip_reason == "llm_error"


def test_no_key_skips_without_llm_call(monkeypatch):
    for k in ("DEEPSEEK_API_KEY", "ZHIPU_API_KEY", "GLM_API_KEY", "XAI_API_KEY", "GROK_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    outcome = run_precheck("合同正文", "lease", chat_fn=None)
    assert outcome.skip_reason == "no_llm_key"
    assert outcome.performed is False


def test_disabled_flag_skips(monkeypatch):
    monkeypatch.setenv("PRECHECK_ENABLED", "false")
    outcome = run_precheck("合同正文", "lease", chat_fn=_chat_returns(json.dumps(GOOD), []))
    assert outcome.skip_reason == "disabled"


def test_busy_degrades_without_calling(monkeypatch):
    """并发槽占满 → busy 降级跳过，不排队不调用（肉饼门禁 P1 整改）。"""
    monkeypatch.setattr(
        precheck, "_precheck_slots", __import__("threading").BoundedSemaphore(0)
    )
    outcome = run_precheck("合同正文", "lease", chat_fn=_chat_returns(json.dumps(GOOD), []))
    assert outcome.skip_reason == "busy"
    assert outcome.performed is False


def test_default_chat_fn_provider_order(monkeypatch):
    """供应商链 DeepSeek > 智谱 > xAI（肉饼门禁 P3-4：选择路径此前零覆盖）。"""
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("ZHIPU_API_KEY", raising=False)
    monkeypatch.delenv("GLM_API_KEY", raising=False)
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.delenv("GROK_API_KEY", raising=False)

    assert precheck._default_chat_fn() is None

    monkeypatch.setenv("ZHIPU_API_KEY", "k-zhipu")
    monkeypatch.setenv("XAI_API_KEY", "k-xai")
    fn = precheck._default_chat_fn()
    assert fn is not None
    # 智谱在 xAI 之前：用捕获参数验证走的是 _chat_zhipu 且带预审短超时
    captured = {}
    monkeypatch.setattr(
        precheck.llm_ask,
        "_chat_zhipu",
        lambda key, s, u, timeout=None: captured.update(key=key, timeout=timeout) or "raw",
    )
    assert precheck._default_chat_fn()("sys", "usr") == "raw"
    assert captured["key"] == "k-zhipu"
    assert captured["timeout"] == precheck.DEFAULT_PRECHECK_TIMEOUT

    monkeypatch.setenv("DEEPSEEK_API_KEY", "k-ds")
    captured_ds = {}
    monkeypatch.setattr(
        precheck.llm_ask,
        "_chat_deepseek",
        lambda key, s, u, timeout=None: captured_ds.update(key=key, timeout=timeout) or "raw",
    )
    assert precheck._default_chat_fn()("sys", "usr") == "raw"
    assert captured_ds["key"] == "k-ds", "DeepSeek 必须优先于智谱"
    assert captured_ds["timeout"] == precheck.DEFAULT_PRECHECK_TIMEOUT


# ---------- 禁语与采样 ----------

def test_summary_banned_phrase_scrubbed():
    payload = GOOD | {"summary": "这份合同没问题，租金条款无风险"}
    outcome = run_precheck("合同正文", "lease", chat_fn=_chat_returns(json.dumps(payload), []))
    assert "没问题" not in outcome.result.summary
    assert "无风险" not in outcome.result.summary


def test_detected_type_scrubbed_and_capped():
    """类型名同样过禁语清洗且限长（肉饼门禁 P3-1）。"""
    payload = GOOD | {"detected_type": "没问题的租赁合同" + "长" * 200}
    outcome = run_precheck("合同正文", "lease", chat_fn=_chat_returns(json.dumps(payload), []))
    assert "没问题" not in outcome.result.detected_type
    assert len(outcome.result.detected_type) <= 100
    payload2 = GOOD | {"summary": "概" * 500}
    outcome2 = run_precheck("合同正文", "lease", chat_fn=_chat_returns(json.dumps(payload2), []))
    assert len(outcome2.result.summary) <= 300


def test_prompt_clip_keeps_head_and_tail():
    calls: list = []
    long_text = "开头主体条款。" + "甲" * 5000 + "尾部签署区。"
    run_precheck(long_text, "lease", chat_fn=_chat_returns(json.dumps(GOOD), calls))
    user = calls[0][1]
    assert len(user) < 5000 + 200
    assert "开头主体条款" in user and "尾部签署区" in user
    assert "中段截断" in user


# ---------- decide_branch 对账矩阵（spec 3.3 + 老钱 low 改判） ----------

def _outcome(**kw) -> PrecheckOutcome:
    return PrecheckOutcome(performed=True, result=PrecheckResult(**{**GOOD, **kw}))


def test_branch_same_category_proceeds():
    assert decide_branch(_outcome(), "lease") == {"action": "proceed", "suspect": False}


def test_branch_unsupported():
    assert decide_branch(_outcome(is_supported=False, suggested_category=None), "lease") == {
        "action": "confirm_unsupported",
        "suspect": False,
    }


def test_branch_supported_mismatch_confirms_switch():
    assert decide_branch(_outcome(suggested_category="nda"), "lease") == {
        "action": "confirm_switch",
        "suspect": False,
    }


def test_branch_low_confidence_mismatch_proceeds_with_suspect():
    """老钱改判：知情权不能省，打断权必须不给。"""
    assert decide_branch(_outcome(suggested_category="procurement", confidence="low"), "lease") == {
        "action": "proceed",
        "suspect": True,
    }


def test_branch_skip_outcome_proceeds():
    assert decide_branch(PrecheckOutcome(skip_reason="no_llm_key"), "lease") == {
        "action": "proceed",
        "suspect": False,
    }
