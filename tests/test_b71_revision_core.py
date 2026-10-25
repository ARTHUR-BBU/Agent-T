# -*- coding: utf-8 -*-
"""B7-1 三版改写核心链测试（spec-b7-1-revision-core.md §三）。

硬事清单（开工令 2026-10-24）：额度按审查单持久累计（重启不重领）、并发点击
不超发、三版缺一版整项不展示、建议有原文依据与三层留痕、绝不改写规则判定。
全程 mock，零 LLM 花费。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

import pytest

from app.services import revision
from app.services.store import ReviewStore

_GOOD = json.dumps({
    "versions": [
        {"stance": "稳妥", "text": "改为对等罚则表述。", "note": "最容易谈成"},
        {"stance": "平衡", "text": "折中方案文本。", "note": "双方各让一步"},
        {"stance": "进取", "text": "进取方案文本。", "note": "我方强势时用"},
    ],
    "rationale": "三版差异说明。",
}, ensure_ascii=False)


@pytest.fixture()
def env_store(tmp_path, monkeypatch):
    monkeypatch.setenv("STORE_DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.delenv("LLM_REVISION_BUDGET_PER_REVIEW", raising=False)
    monkeypatch.delenv("LLM_IMPACT_BUDGET_PER_REVIEW", raising=False)
    # 默认有 Key（走 llm_call 注入路径）；no-key 用例单独覆写为 None
    monkeypatch.setattr(revision.llm_ask, "get_api_key",
                        lambda user_id=None: "test-key")
    store = ReviewStore(db_path=str(tmp_path / "t.db"))
    rid = store.create(filename="c.docx", category="procurement")
    store.update(rid, status="done", items=[
        {"id": "unfair_terms", "name": "公平性", "status": "需关注",
         "note": "定金单方没收", "quote": "乙方违约的定金不予退还。",
         "hits": ["不予退还"]},
        {"id": "term", "name": "期限", "status": "未找到", "note": "x",
         "quote": "", "hits": []},
    ])
    return store, rid


def _ok_call(observed: dict | None = None):
    def call(api_key: str, system: str, user: str) -> str:
        if observed is not None:
            observed["system"] = system
            observed["user"] = user
        return _GOOD
    return call


# ---------- 红线 2：只对已打标 item，未检出拒绝且不扣额度 ----------

def test_unknown_item_refused_without_llm_or_budget(env_store):
    store, rid = env_store
    llm = mock.Mock()
    with mock.patch.object(revision.llm_call_log, "emit_skip") as skip:
        r = revision.generate_revision(store, rid, "not_in_review", llm_call=llm)
    assert r["status"] == "not_found"
    llm.assert_not_called()
    skip.assert_not_called()
    assert revision.remaining_budget(rid, "revision", 4) == 4, "拒绝不得扣额度"


def test_unknown_review_404_shape(env_store):
    store, _ = env_store
    assert revision.generate_revision(store, "ghost", "unfair_terms")[
        "status"] == "not_found"


# ---------- 额度：持久累计 / 重启不重领 / 并发不超发 ----------

def test_budget_accrues_across_requests_and_persists_restart(env_store):
    """跨请求累计+重启不重领：4 次额度分 4 次独立调用耗尽，第 5 次必拒；
    额度在 SQLite（随审查单库），新连接（=模拟重启后进程）读到的余量不变。"""
    store, rid = env_store
    for i in range(4):
        r = revision.generate_revision(store, rid, "unfair_terms",
                                       llm_call=_ok_call())
        assert r["status"] == "ok", f"第 {i+1} 次应成功"
    # 模拟重启：全新连接直查（模块函数本身无进程内计数状态）
    con = sqlite3.connect(str(store._path))
    row = con.execute("SELECT revision_used FROM revision_budget WHERE review_id=?",
                      (rid,)).fetchone()
    con.close()
    assert row == (4,), "额度必须持久落库（内存计数=重启重领，不 acceptance）"
    with mock.patch.object(revision.llm_call_log, "emit_skip") as skip:
        r = revision.generate_revision(store, rid, "unfair_terms",
                                       llm_call=_ok_call())
    assert r["status"] == "budget_exceeded"
    skip.assert_called_once(), "拒绝必须 emit_skip 显式落账"
    llm = mock.Mock()
    r = revision.generate_revision(store, rid, "unfair_terms", llm_call=llm)
    llm.assert_not_called(), "预算拒绝=零网络请求"


def test_concurrent_clicks_never_overshoot(env_store):
    """并发点击原子扣减：限额 4，8 个并发至多 4 个成功（不超发）。"""
    store, rid = env_store
    results: list[str] = []
    lock = threading.Lock()

    def click(_: int) -> None:
        r = revision.generate_revision(store, rid, "unfair_terms",
                                       llm_call=_ok_call())
        with lock:
            results.append(r["status"])

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(click, range(8)))
    assert results.count("ok") == 4, f"恰好 4 个成功，实际 {results}"
    assert results.count("budget_exceeded") == 4


def test_budget_env_override_respected(env_store, monkeypatch):
    monkeypatch.setenv("LLM_REVISION_BUDGET_PER_REVIEW", "2")
    store, rid = env_store
    assert revision.generate_revision(store, rid, "unfair_terms",
                                      llm_call=_ok_call())["status"] == "ok"
    assert revision.generate_revision(store, rid, "unfair_terms",
                                      llm_call=_ok_call())["status"] == "ok"
    assert revision.generate_revision(store, rid, "unfair_terms",
                                      llm_call=_ok_call())["status"] == \
        "budget_exceeded"


# ---------- 三版完整性：缺一版整项降级，不展示残缺 ----------

def _degraded(env_store, raw: str, expect_reason: str):
    store, rid = env_store
    r = revision.generate_revision(store, rid, "unfair_terms",
                                   llm_call=lambda *a: raw)
    assert r["status"] == "degraded" and r["reason"] == expect_reason
    d = revision.latest_draft(rid, "unfair_terms")
    assert d is not None and d["status"] == "degraded"
    assert not d["payload"].get("versions"), "降级不得展示残缺方案"
    return r


def test_missing_one_version_degrades_whole_item(env_store):
    two = json.dumps({"versions": _GOOD and json.loads(_GOOD)["versions"][:2],
                      "rationale": "x"}, ensure_ascii=False)
    _degraded(env_store, two, "incomplete")


def test_empty_version_text_degrades(env_store):
    bad = json.dumps({"versions": [
        {"stance": "稳妥", "text": "", "note": ""},
        {"stance": "平衡", "text": "x", "note": ""},
        {"stance": "进取", "text": "y", "note": ""}],
        "rationale": "x"}, ensure_ascii=False)
    _degraded(env_store, bad, "incomplete")


def test_invalid_json_degrades(env_store):
    _degraded(env_store, "这不是 JSON", "invalid_json")


def test_llm_error_degrades_honestly(env_store):
    def boom(*a):
        raise RuntimeError("network down")
    store, rid = env_store
    r = revision.generate_revision(store, rid, "unfair_terms", llm_call=boom)
    assert r["status"] == "degraded" and r["reason"] == "llm_error"


# ---------- 红线 1：禁语过滤 + 声明 ----------

def test_banned_phrase_degrades(env_store):
    bad = json.dumps({"versions": [
        {"stance": "稳妥", "text": "建议签署本版本。", "note": ""},
        {"stance": "平衡", "text": "x", "note": ""},
        {"stance": "进取", "text": "y", "note": ""}],
        "rationale": "x"}, ensure_ascii=False)
    _degraded(env_store, bad, "redline_banned:建议签署")


def test_ok_payload_carries_declaration_and_three_layers(env_store):
    store, rid = env_store
    observed: dict = {}
    r = revision.generate_revision(store, rid, "unfair_terms",
                                   llm_call=_ok_call(observed))
    assert r["status"] == "ok"
    d = r["draft"]
    assert d["declaration"], "生成物必须携带非法律意见声明"
    assert "不构成法律意见" in d["declaration"]
    # 三层留痕：原引句+修改文本+理由
    assert d["original_quote"] == "乙方违约的定金不予退还。"
    assert len(d["versions"]) == 3 and all(v["text"] for v in d["versions"])
    assert d["rationale"]
    assert "定金不予退还" in observed["user"], "提示词必须携带原文依据"
    assert "禁止" in observed["system"] and "JSON" in observed["system"]
    assert r["trace_id"], "每次交互调用必须有 trace_id（账本可查）"


# ---------- 红线 4：建议绝不改写规则判定 ----------

def test_items_untouched_by_generation(env_store):
    store, rid = env_store
    before = json.dumps(store.get(rid)["items"], ensure_ascii=False,
                        sort_keys=True)
    revision.generate_revision(store, rid, "unfair_terms", llm_call=_ok_call())
    after = json.dumps(store.get(rid)["items"], ensure_ascii=False,
                       sort_keys=True)
    assert before == after, "生成建议不得改变规则判定（物理隔离）"


def test_llm_key_missing_degrades(env_store, monkeypatch):
    store, rid = env_store
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("ZHIPU_API_KEY", raising=False)
    monkeypatch.delenv("GLM_API_KEY", raising=False)
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.delenv("GROK_API_KEY", raising=False)
    with mock.patch.object(revision.llm_ask, "get_api_key", return_value=None):
        r = revision.generate_revision(store, rid, "unfair_terms",
                                       llm_call=_ok_call())
    assert r["status"] == "degraded" and r["reason"] == "no_llm_key"
