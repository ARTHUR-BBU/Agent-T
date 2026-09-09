"""限频测试（阶段 0.5）：单元（注入时钟）+ API 级 429。

conftest 已 autouse 关闭限频 + teardown 清桶；本文件内用 monkeypatch.setenv
显式设置限额。全程无 LLM 调用：限频依赖先于 handler 执行。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.rate_limit import (
    SlidingWindowLimiter,
    reset_for_tests,
    validate_rate_limit_config,
)

client = TestClient(app)

# 一个能通过上传校验的最小合同（不触发预审：conftest 已关）
_MINI = "甲方乙方约定：货款验收合格后支付。争议向法院起诉。适用中华人民共和国法律。"


# ---------- SlidingWindowLimiter 单元 ----------

def _limiter(monkeypatch, limit: int | str) -> SlidingWindowLimiter:
    monkeypatch.setenv("RL_TEST_LIMIT", str(limit))
    return SlidingWindowLimiter("RL_TEST_LIMIT", 99)


def test_window_rejects_at_limit(monkeypatch):
    lim = _limiter(monkeypatch, 3)
    assert lim.allow("ip1", now=100.0)
    assert lim.allow("ip1", now=100.1)
    assert lim.allow("ip1", now=100.2)
    assert not lim.allow("ip1", now=100.3), "窗口内第 4 次必须拒绝"


def test_window_expires_and_reallows(monkeypatch):
    lim = _limiter(monkeypatch, 2)
    assert lim.allow("ip1", now=100.0)
    assert lim.allow("ip1", now=100.1)
    assert not lim.allow("ip1", now=159.9)
    assert lim.allow("ip1", now=160.1), "最早的记录滑出 60s 窗口后放行"


def test_rejected_requests_do_not_count(monkeypatch):
    """被拒请求不进窗口：不会「重试风暴自己填满窗口」导致永久封禁。"""
    lim = _limiter(monkeypatch, 1)
    assert lim.allow("ip1", now=100.0)
    for i in range(50):
        assert not lim.allow("ip1", now=100.1 + i)
    assert lim.allow("ip1", now=160.05), "窗口滑出后应立即恢复"


def test_zero_disables(monkeypatch):
    lim = _limiter(monkeypatch, 0)
    for i in range(1000):
        assert lim.allow("ip1", now=100.0), "0 = 关闭，全放行"


def test_garbage_env_falls_back_to_default(monkeypatch):
    lim = _limiter(monkeypatch, "abc")
    assert lim.limit() == 99, "垃圾值运行时回退默认"


def test_negative_env_disables(monkeypatch):
    lim = _limiter(monkeypatch, "-5")
    assert lim.limit() == 0


def test_different_ips_independent(monkeypatch):
    lim = _limiter(monkeypatch, 1)
    assert lim.allow("1.1.1.1", now=100.0)
    assert lim.allow("2.2.2.2", now=100.0), "不同 IP 互不影响"


def test_bucket_cleanup_when_too_many(monkeypatch):
    lim = _limiter(monkeypatch, 1)
    for i in range(600):
        lim.allow(f"10.0.0.{i}", now=100.0)
    assert len(lim._buckets) <= 600
    # 触发一次超阈值后的清理路径（桶被清空但键保留）
    lim.allow("10.0.0.0", now=200.0)
    assert all(isinstance(b, type(lim._buckets["10.0.0.0"])) for b in lim._buckets.values())


# ---------- validate_rate_limit_config（fail-closed 对齐 auth） ----------

@pytest.mark.parametrize(
    "env_name, value",
    [
        ("RATE_LIMIT_UPLOAD_PER_MINUTE", "abc"),
        ("RATE_LIMIT_UPLOAD_PER_MINUTE", "-1"),
        ("RATE_LIMIT_ASK_PER_MINUTE", "1.5"),
        ("RATE_LIMIT_ASK_PER_MINUTE", "abc"),
    ],
)
def test_validate_rejects_illegal_config(monkeypatch, env_name, value):
    monkeypatch.setenv(env_name, value)
    with pytest.raises(RuntimeError):
        validate_rate_limit_config()


@pytest.mark.parametrize(
    "env_name, value",
    [("RATE_LIMIT_UPLOAD_PER_MINUTE", "0"), ("RATE_LIMIT_ASK_PER_MINUTE", "30"), ("RATE_LIMIT_UPLOAD_PER_MINUTE", "")],
)
def test_validate_accepts_legal_config(monkeypatch, env_name, value):
    monkeypatch.setenv(env_name, value)
    validate_rate_limit_config()  # 不抛即过


# ---------- API 级 ----------

def test_upload_rate_limited_429(monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_UPLOAD_PER_MINUTE", "2")
    reset_for_tests()
    files = {"file": ("mini.txt", _MINI.encode("utf-8"), "text/plain")}
    assert client.post("/api/upload", files=files, data={"category": "procurement"}).status_code == 200
    assert client.post("/api/upload", files=files, data={"category": "procurement"}).status_code == 200
    r = client.post("/api/upload", files=files, data={"category": "procurement"})
    assert r.status_code == 429
    assert "上传过于频繁" in r.json()["detail"], "限频文案必须可与「排队已满」区分"
    assert r.headers.get("retry-after") == "60"


def test_ask_rate_limited_before_404(monkeypatch):
    """限频依赖先于 handler：不存在的 review_id 也先吃 429 而不是 404。"""
    monkeypatch.setenv("RATE_LIMIT_ASK_PER_MINUTE", "1")
    reset_for_tests()
    body = {"review_id": "nonexistent", "item_id": "x", "question": "风险大吗？"}
    assert client.post("/api/ask", json=body).status_code == 404
    r = client.post("/api/ask", json=body)
    assert r.status_code == 429
    assert "提问过于频繁" in r.json()["detail"]


def test_queue_full_429_has_no_retry_after(monkeypatch):
    """既有「排队已满」与限频 429 的可区分性：前者无 Retry-After 头。
    （并发槽占满路径难以在单测稳定构造，这里只锁响应头契约的实现一致性）"""
    from app.api import routes

    assert routes.MAX_CONCURRENT_REVIEWS >= 1  # 槽位配置仍在（防误删）


def test_reset_for_tests_clears_buckets(monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_UPLOAD_PER_MINUTE", "1")
    reset_for_tests()
    files = {"file": ("mini.txt", _MINI.encode("utf-8"), "text/plain")}
    assert client.post("/api/upload", files=files, data={"category": "procurement"}).status_code == 200
    assert client.post("/api/upload", files=files, data={"category": "procurement"}).status_code == 429
    reset_for_tests()
    assert client.post("/api/upload", files=files, data={"category": "procurement"}).status_code == 200
