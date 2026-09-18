"""LLM 调用账本测试（宪法 P0-D1）。

覆盖：emit/query 往返、TTL 清理、record_node 上下文注入、
transport 接线（真实 chat_completion 走 mock httpx 后落账）、
账本故障不影响主链。
"""
from __future__ import annotations

import pytest

from app.services import llm_call_log
from app.services.llm_call_log import (
    LLMCallRecord,
    emit,
    new_call_id,
    purge_expired,
    query,
    record_node,
)


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    """账本落到临时库（不污染默认 store 库）。"""
    monkeypatch.setenv("STORE_DB_PATH", str(tmp_path / "calls.db"))
    yield


def test_emit_query_roundtrip(isolated_db):
    rid = new_call_id()
    emit(LLMCallRecord(
        call_id=rid, node="objection", provider="deepseek", model="test-model",
        prompt_version="sha-abc", chars_sent=1234, truncated=False,
        review_id="r1", latency_ms=88, outcome="success",
        usage={"input_tokens": 10, "output_tokens": 5, "cost": None},
        gate_counts={"accepted": 2, "rejected": 1},
    ))
    rows = query(review_id="r1")
    assert len(rows) == 1
    row = rows[0]
    assert row["node"] == "objection"
    assert row["chars_sent"] == 1234
    assert row["usage"]["input_tokens"] == 10
    assert row["gate_counts"]["rejected"] == 1


def test_purge_expired(isolated_db):
    emit(LLMCallRecord(call_id=new_call_id(), node="ask", provider="zhipu", model="m"))
    assert purge_expired(max_age_seconds=3600) == 0  # 新记录不删
    assert purge_expired(max_age_seconds=0) >= 1     # 全删


def test_record_node_context(isolated_db):
    with record_node("quality", review_id="r9"):
        assert llm_call_log.current_node() == "quality"
        assert llm_call_log.current_review_id() == "r9"
    assert llm_call_log.current_node() is None


def test_transport_writes_record(isolated_db, monkeypatch):
    """transport 真实路径（mock httpx）落账：success + usage 记录。"""
    from app.services import llm_client

    class FakeResp:
        status_code = 200
        def raise_for_status(self):
            pass
        def json(self):
            return {
                "choices": [{"message": {"content": "好的答案"}}],
                "usage": {"prompt_tokens": 11, "completion_tokens": 7},
            }

    class FakeClient:
        def __init__(self, timeout):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def post(self, url, headers=None, json=None):
            return FakeResp()

    monkeypatch.setattr(llm_client.httpx, "Client", FakeClient)
    out = llm_client.chat_completion(
        api_key="k", url="https://api.deepseek.com/v1/chat/completions",
        model="test-model", system="sys", user="user-content",
        temperature=0.2, timeout=5,
    )
    assert out == "好的答案"
    rows = query(limit=10)
    assert rows, "transport 调用必须落账"
    rec = rows[0]
    assert rec["outcome"] == "success"
    assert rec["provider"] == "deepseek"
    assert rec["usage"]["output_tokens"] == 7
    assert rec["latency_ms"] is not None


def test_transport_provider_error_recorded(isolated_db, monkeypatch):
    """供应商 4xx/5xx：provider_error 也落账（失败在哪里要能回答）。"""
    import httpx as real_httpx
    from app.services import llm_client

    class FakeResp:
        status_code = 500
        text = "boom"
        request = None
        response = None

    class FakeClient:
        def __init__(self, timeout):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def post(self, url, headers=None, json=None):
            raise real_httpx.HTTPStatusError("500 boom", request=None, response=FakeResp())

    monkeypatch.setattr(llm_client.httpx, "Client", FakeClient)
    with pytest.raises(real_httpx.HTTPStatusError):
        llm_client.chat_completion(
            api_key="k", url="https://api.zhipu.ai/chat", model="m",
            system="s", user="u", temperature=0.2, timeout=5,
        )
    rows = query(limit=10)
    assert rows and rows[0]["outcome"] == "provider_error"
    assert rows[0]["provider"] == "zhipu"


def test_emit_failure_never_raises(isolated_db, monkeypatch):
    """账本故障不影响主链：DB 不可写时 emit 静默。"""
    monkeypatch.setenv("STORE_DB_PATH", "Z:/nonexistent-dir/x.db")
    emit(LLMCallRecord(call_id=new_call_id(), node="ask", provider="zhipu", model="m"))
    # 不 raise 即通过
