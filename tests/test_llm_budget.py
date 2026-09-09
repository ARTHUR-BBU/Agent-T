"""单次审查 LLM 预算测试（阶段 0.5）。

覆盖：ReviewBudget 原子性/并发、env 解析矩阵、run_precheck 与
run_model_review 的软降级路径、pipeline 透传。全程无真实 LLM。
"""
from __future__ import annotations

import json
import threading

import pytest

from app.graph import pipeline as pipeline_mod
from app.services import llm_budget, precheck as precheck_service
from app.services.llm_budget import DEFAULT_BUDGET, ReviewBudget, limit_from_env, new_review_budget
from app.services.model_review import run_model_review


# ---------- ReviewBudget 基础 ----------

def test_try_consume_basic():
    b = ReviewBudget(2)
    assert b.try_consume()
    assert b.try_consume()
    assert not b.try_consume()
    assert not b.try_consume(), "耗尽后持续拒绝"


def test_no_partial_consume_when_insufficient():
    b = ReviewBudget(1)
    assert not b.try_consume(2), "余额不足不得部分扣减"
    assert b.try_consume(), "额度应完整保留"


def test_zero_limit_is_unlimited():
    b = ReviewBudget(0)
    for _ in range(100):
        assert b.try_consume(), "limit<=0 = 不限"
    assert b.remaining() == -1


def test_concurrent_try_consume_exactly_exhausts():
    """50 线程抢 50 份额：成功数必须恰好 50（原子 check-and-decrement）。"""
    b = ReviewBudget(50)
    barrier = threading.Barrier(50)
    results: list[bool] = []
    lock = threading.Lock()

    def worker():
        barrier.wait()
        ok = b.try_consume()
        with lock:
            results.append(ok)

    threads = [threading.Thread(target=worker) for _ in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count(True) == 50
    assert b.remaining() == 0


# ---------- env 解析 ----------

def test_limit_from_env_unset(monkeypatch):
    monkeypatch.delenv("LLM_BUDGET_PER_REVIEW", raising=False)
    assert limit_from_env() == DEFAULT_BUDGET == 12


def test_limit_from_env_zero_disables(monkeypatch):
    monkeypatch.setenv("LLM_BUDGET_PER_REVIEW", "0")
    assert limit_from_env() == 0


def test_limit_from_env_garbage_falls_back(monkeypatch):
    monkeypatch.setenv("LLM_BUDGET_PER_REVIEW", "abc")
    assert limit_from_env() == DEFAULT_BUDGET


def test_limit_from_env_custom(monkeypatch):
    monkeypatch.setenv("LLM_BUDGET_PER_REVIEW", "5")
    assert limit_from_env() == 5


def test_new_review_budget_disabled_returns_none(monkeypatch):
    monkeypatch.setenv("LLM_BUDGET_PER_REVIEW", "0")
    assert new_review_budget() is None


def test_new_review_budget_enabled(monkeypatch):
    monkeypatch.delenv("LLM_BUDGET_PER_REVIEW", raising=False)
    b = new_review_budget()
    assert b is not None and b.remaining() == DEFAULT_BUDGET


# ---------- run_precheck 预算检查点 ----------

_GOOD = json.dumps(
    {
        "detected_type": "房屋租赁合同",
        "is_supported": True,
        "suggested_category": "lease",
        "confidence": "high",
        "summary": "常规租赁合同",
    }
)


def test_precheck_budget_exhausted_first_round(monkeypatch):
    monkeypatch.setenv("PRECHECK_ENABLED", "true")
    outcome = precheck_service.run_precheck(
        "合同正文", "lease",
        chat_fn=lambda s, u: _GOOD,
        budget=_exhausted(),
    )
    assert outcome.skip_reason == "budget_exceeded"
    assert outcome.performed is False


def _exhausted() -> ReviewBudget:
    b = ReviewBudget(1)
    b.try_consume()
    return b


def test_precheck_retry_skipped_when_budget_gone(monkeypatch):
    """首轮解析失败（该消耗 1 次已耗尽）→ 重试前预算尽 → budget_exceeded
    而不是 parse_failed（检查点在重试循环内的意义所在）。"""
    monkeypatch.setenv("PRECHECK_ENABLED", "true")
    calls = {"n": 0}

    def chat(s, u):
        calls["n"] += 1
        return "不是 JSON"

    b = ReviewBudget(1)
    outcome = precheck_service.run_precheck("合同正文", "lease", chat_fn=chat, budget=b)
    assert calls["n"] == 1, "预算尽不得发起第二次调用"
    assert outcome.skip_reason == "budget_exceeded"


def test_precheck_budget_sufficient_passes(monkeypatch):
    monkeypatch.setenv("PRECHECK_ENABLED", "true")
    outcome = precheck_service.run_precheck(
        "合同正文", "lease", chat_fn=lambda s, u: _GOOD, budget=ReviewBudget(5)
    )
    assert outcome.performed is True
    assert outcome.result is not None


def test_precheck_none_budget_is_passthrough(monkeypatch):
    """关闭态（None）零开销直通：budget_exceeded 路径不触发。"""
    monkeypatch.setenv("PRECHECK_ENABLED", "true")
    outcome = precheck_service.run_precheck(
        "合同正文", "lease", chat_fn=lambda s, u: _GOOD, budget=None
    )
    assert outcome.performed is True


# ---------- run_model_review 预算检查点 ----------

_ITEMS = [
    {"id": "payment", "name": "价款与支付", "status": "需关注", "note": "n", "quote": "q"},
    {"id": "term", "name": "期限", "status": "通过", "note": "", "quote": ""},
]


def test_model_review_budget_exhausted_degrades(monkeypatch):
    monkeypatch.delenv("BLIND_SPOT_ENABLED", raising=False)
    result = run_model_review(
        text="合同正文", items=_ITEMS, category="procurement",
        chat_fn=lambda s, u: "{}", budget=_exhausted(),
    )
    sc = result["scorecard"]
    assert sc["available"] is False
    assert sc["reason"] == "budget_exceeded"
    assert result["blind_skipped_reason"] == "budget_exceeded"


def test_model_review_retry_blocked_by_budget(monkeypatch):
    """首轮禁语命中（消耗掉最后额度）→ 重试前预算尽 → budget_exceeded。"""
    monkeypatch.delenv("BLIND_SPOT_ENABLED", raising=False)
    banned = json.dumps(
        {"scorecard": {"total": 90, "summary": "这份合同没有问题，可以放心签署"}}
    )
    calls = {"n": 0}

    def chat(s, u):
        calls["n"] += 1
        return banned

    result = run_model_review(
        text="合同正文", items=_ITEMS, category="procurement",
        chat_fn=chat, budget=ReviewBudget(1),
    )
    assert calls["n"] == 1, "预算尽不得发起重试调用"
    assert result["scorecard"]["reason"] == "budget_exceeded"


def test_model_review_budget_sufficient_normal(monkeypatch):
    monkeypatch.delenv("BLIND_SPOT_ENABLED", raising=False)
    payload = json.dumps(
        {"scorecard": {"total": 80, "segments": [], "summary": "参考评分，仅供决策参考"}}
    )
    result = run_model_review(
        text="合同正文", items=_ITEMS, category="procurement",
        chat_fn=lambda s, u: payload, budget=ReviewBudget(5),
    )
    assert result["scorecard"].get("available") is True


# ---------- pipeline 透传 ----------

def test_pipeline_node_passes_budget_to_model_review(monkeypatch):
    """node_model_review 必须把 state 里的 budget 传给 run_model_review。"""
    captured = {}
    monkeypatch.setattr(
        pipeline_mod, "run_model_review",
        lambda **kw: captured.update(budget=kw.get("budget")) or {"scorecard": {}},
    )
    b = ReviewBudget(3)
    pipeline_mod.node_model_review({"text": "t", "items": [], "budget": b})
    assert captured["budget"] is b
    # 无 budget 的 state（旧行为）也不得报错
    pipeline_mod.node_model_review({"text": "t", "items": []})


def test_run_review_signature_accepts_budget():
    """run_review(budget=...) 端到端：无规则结果时评分门禁在预算之前，
    这里只锁签名兼容（预算耗尽的端到端行为由 node 级测试覆盖）。"""
    import inspect

    sig = inspect.signature(pipeline_mod.run_review)
    assert "budget" in sig.parameters
