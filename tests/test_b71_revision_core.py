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
    "assumptions": ["假设定金给付方为乙方"],
}, ensure_ascii=False)


@pytest.fixture()
def env_store(tmp_path, monkeypatch):
    monkeypatch.setenv("STORE_DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.delenv("LLM_REVISION_BUDGET_PER_REVIEW", raising=False)
    monkeypatch.delenv("LLM_IMPACT_BUDGET_PER_REVIEW", raising=False)
    # 默认有 DeepSeek Key（走 llm_call 注入路径）；no-key 用例单独覆写为 None
    monkeypatch.setattr(revision.llm_ask, "_deepseek_key",
                        lambda: "test-ds-key")
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
        {"stance": "稳妥", "text": "建议签署本版本。", "note": "n1"},
        {"stance": "平衡", "text": "x", "note": "n2"},
        {"stance": "进取", "text": "y", "note": "n3"}],
        "rationale": "x", "assumptions": []}, ensure_ascii=False)
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
    # 三层留痕+假设层（B7-2a 前置一）：原引句+修改文本+理由+显式假设
    assert d["original_quote"] == "乙方违约的定金不予退还。"
    assert len(d["versions"]) == 3 and all(v["text"] for v in d["versions"])
    assert d["rationale"]
    assert d["assumptions"] == ["假设定金给付方为乙方"]
    assert "定金不予退还" in observed["user"], "提示词必须携带原文依据"
    assert "禁止" in observed["system"] and "JSON" in observed["system"]
    assert "assumptions" in observed["system"], "提示词必须要求显式假设"
    assert "全合同无相关约定" in observed["system"],         "全卷性断言禁令必须在提示词中（局部引句视野边界）"
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
    # P2-2：只认 DeepSeek 凭据——仅有智谱 Key 时不得发给 DeepSeek 通道
    monkeypatch.setenv("ZHIPU_API_KEY", "zhipu-only")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with mock.patch.object(revision.llm_ask, "_deepseek_key", return_value=None):
        llm = mock.Mock()
        r = revision.generate_revision(store, rid, "unfair_terms",
                                       llm_call=llm)
    assert r["status"] == "degraded" and r["reason"] == "no_llm_key"
    llm.assert_not_called()


# ---------- P1-1：不可改写项拒绝（未找到/无原文），不扣额度不调 LLM ----------

def test_not_found_item_not_revisable(env_store):
    store, rid = env_store
    llm = mock.Mock()
    r = revision.generate_revision(store, rid, "term", llm_call=llm)
    assert r["status"] == "not_supported"
    assert r["reason"] == "item_not_revisable"
    llm.assert_not_called()
    assert revision.remaining_budget(rid, "revision", 4) == 4, "拒绝不得扣额度"


def test_empty_quote_item_not_revisable(env_store):
    store, rid = env_store
    store.update(rid, items=[
        {"id": "blank", "name": "空引句项", "status": "需关注",
         "note": "n", "quote": "", "hits": []}])
    llm = mock.Mock()
    r = revision.generate_revision(store, rid, "blank", llm_call=llm)
    assert r["status"] == "not_supported"
    llm.assert_not_called()


def test_pass_item_not_revisable(env_store):
    """通过项无可「改」对象——修改建议只服务已检出问题（需关注）。"""
    store, rid = env_store
    store.update(rid, items=[
        {"id": "fine", "name": "没问题", "status": "通过",
         "note": "n", "quote": "有原文。", "hits": []}])
    r = revision.generate_revision(store, rid, "fine", llm_call=_ok_call())
    assert r["status"] == "not_supported"


# ---------- P2-1：严格 Schema（版型/类型/非空） ----------

def test_duplicate_stances_degrade(env_store):
    bad = json.dumps({"versions": [
        {"stance": "稳妥", "text": "a", "note": "n"},
        {"stance": "稳妥", "text": "b", "note": "n"},
        {"stance": "进取", "text": "c", "note": "n"}],
        "rationale": "x"}, ensure_ascii=False)
    _degraded(env_store, bad, "incomplete")


def test_wrong_stance_names_degrade(env_store):
    bad = json.dumps({"versions": [
        {"stance": "保守", "text": "a", "note": "n"},
        {"stance": "中间", "text": "b", "note": "n"},
        {"stance": "激进", "text": "c", "note": "n"}],
        "rationale": "x"}, ensure_ascii=False)
    _degraded(env_store, bad, "incomplete")


def test_numeric_text_degrades(env_store):
    bad = json.dumps({"versions": [
        {"stance": "稳妥", "text": 42, "note": "n"},
        {"stance": "平衡", "text": "b", "note": "n"},
        {"stance": "进取", "text": "c", "note": "n"}],
        "rationale": "x"}, ensure_ascii=False)
    _degraded(env_store, bad, "incomplete")


def test_missing_note_degrades(env_store):
    bad = json.dumps({"versions": [
        {"stance": "稳妥", "text": "a"},
        {"stance": "平衡", "text": "b", "note": "n"},
        {"stance": "进取", "text": "c", "note": "n"}],
        "rationale": "x"}, ensure_ascii=False)
    _degraded(env_store, bad, "incomplete")


# ---------- P1-2：三条真实删除路径都带 assoc 清理（禁手动补调） ----------

def _expire(store, rid) -> None:
    import time as _time
    con = sqlite3.connect(str(store._path))
    con.execute("UPDATE reviews SET created_at = ? WHERE id = ?",
                (_time.time() - 10**9, rid))
    con.commit()
    con.close()


def _leftover(store, rid) -> tuple[int, int]:
    con = sqlite3.connect(str(store._path))
    d = con.execute("SELECT COUNT(*) FROM revision_drafts WHERE review_id=?",
                    (rid,)).fetchone()[0]
    b = con.execute("SELECT COUNT(*) FROM revision_budget WHERE review_id=?",
                    (rid,)).fetchone()[0]
    con.close()
    return d, b


def test_get_path_purges_assoc(env_store):
    """get 过期删除路径：删除后直查库——草稿/预算必须已随之清理
    （禁手动补调清理函数，钩子必须接在真实删除点）。"""
    store, rid = env_store
    revision.generate_revision(store, rid, "unfair_terms", llm_call=_ok_call())
    _expire(store, rid)
    assert store.get(rid) is None  # 真实 get 路径（内含钩子）
    assert _leftover(store, rid) == (0, 0), "get 路径删除后草稿/预算仍残留"
    assert revision.latest_draft(rid, "unfair_terms") is None


def test_update_path_purges_assoc(env_store):
    """update 过期删除路径：同样必须带走关联数据。"""
    store, rid = env_store
    revision.generate_revision(store, rid, "unfair_terms", llm_call=_ok_call())
    _expire(store, rid)
    assert store.update(rid, foo="bar") is None  # 真实 update 路径（内含钩子）
    assert _leftover(store, rid) == (0, 0), "update 路径删除后草稿/预算仍残留"


def test_create_sweep_path_purges_assoc(env_store):
    """create 扫除路径（_purge_expired）：新上传触发的批量清扫必须带走
    已过期审查单的关联数据。"""
    store, rid = env_store
    revision.generate_revision(store, rid, "unfair_terms", llm_call=_ok_call())
    _expire(store, rid)
    store.create(filename="new.docx", category="procurement")  # 真实 create 扫除
    assert _leftover(store, rid) == (0, 0), "create 扫除后草稿/预算仍残留"


def test_latest_draft_self_checks_expiry(env_store):
    """读取草稿自核过期（审查单已过期但尚未被任何路径清扫时也不可读）。"""
    store, rid = env_store
    revision.generate_revision(store, rid, "unfair_terms", llm_call=_ok_call())
    _expire(store, rid)  # 不触发任何 store 删除路径
    assert revision.latest_draft(rid, "unfair_terms") is None, \
        "过期审查单的草稿在清扫前也不得读取（读取时核验过期）"


# ---------- P2-4：业务终态结算 + 证据坐标 ----------

def test_invalid_json_settles_parse_failed(env_store, monkeypatch):
    """P2-4①：走真 transport（mock httpx），200+坏 JSON → 业务终态必须
    结算为 parse_failed（不得停留 transport 的临时 success）。"""
    from app.services import llm_call_log
    store, rid = env_store
    resp = mock.Mock()
    resp.status_code = 200
    resp.json.return_value = {"choices": [{"message": {"content": "坏 JSON"}}],
                              "usage": {}}
    with mock.patch.object(revision.llm_client.httpx, "Client") as fake:
        fake.return_value.__enter__ = lambda s: s
        fake.return_value.__exit__ = lambda s, *a: False
        fake.return_value.post = lambda url, headers=None, json=None, **kw: resp
        r = revision.generate_revision(store, rid, "unfair_terms")
    assert r["status"] == "degraded" and r["reason"] == "invalid_json"
    d = revision.latest_draft(rid, "unfair_terms")
    rows = llm_call_log.query(trace_id=d["trace_id"], limit=5)
    assert rows, "调用行必须存在"
    assert rows[0]["outcome"] == "parse_failed", \
        "业务解析失败必须结算为 parse_failed（不得停留 success）"
    assert "revision:invalid_json" in (rows[0].get("error_detail") or "")


def test_ok_settles_success_with_receipt(env_store):
    from app.services import llm_call_log
    store, rid = env_store
    resp = mock.Mock()
    resp.status_code = 200
    resp.json.return_value = {"choices": [{"message": {"content": _GOOD}}],
                              "usage": {"prompt_tokens": 10,
                                        "completion_tokens": 20}}
    with mock.patch.object(revision.llm_client.httpx, "Client") as fake:
        fake.return_value.__enter__ = lambda s: s
        fake.return_value.__exit__ = lambda s, *a: False
        fake.return_value.post = lambda url, headers=None, json=None, **kw: resp
        r = revision.generate_revision(store, rid, "unfair_terms")
    assert r["status"] == "ok"
    rows = llm_call_log.query(trace_id=r["trace_id"], limit=5)
    assert rows and rows[0]["outcome"] == "success"
    assert rows[0].get("raw_output"), "成功结算必须带回执（已结算标记）"


def test_evidence_coordinates_preserved(env_store):
    """P2-4②：同句多次出现时按坐标定位目标——payload 必须带证据坐标。"""
    store, rid = env_store
    store.update(rid, items=[
        {"id": "unfair_terms", "name": "公平性", "status": "需关注",
         "note": "n", "quote": "乙方违约的定金不予退还。",
         "hits": ["不予退还"],
         "evidence_start": 120, "evidence_end": 133,
         "primary_clause_id": "c7", "clause_ids": ["c7", "c9"]}])
    r = revision.generate_revision(store, rid, "unfair_terms",
                                   llm_call=_ok_call())
    ev = r["draft"]["evidence"]
    assert ev["evidence_start"] == 120 and ev["evidence_end"] == 133
    assert ev["primary_clause_id"] == "c7" and ev["clause_ids"] == ["c7", "c9"]


# ---------- P2-3：配置门禁（正整数强制） ----------

def test_config_gate_rejects_zero_and_garbage(monkeypatch):
    from app.services.config_validate import validate_llm_runtime_config
    monkeypatch.setenv("LLM_REVISION_BUDGET_PER_REVIEW", "0")
    with pytest.raises(ValueError, match="正整数"):
        validate_llm_runtime_config()
    monkeypatch.setenv("LLM_REVISION_BUDGET_PER_REVIEW", "abc")
    with pytest.raises(ValueError, match="不是合法整数"):
        validate_llm_runtime_config()
    monkeypatch.setenv("LLM_REVISION_BUDGET_PER_REVIEW", "4")
    monkeypatch.setenv("LLM_IMPACT_BUDGET_PER_REVIEW", "-1")
    with pytest.raises(ValueError, match="正整数"):
        validate_llm_runtime_config()


def test_runtime_consume_rejects_non_positive_limit(env_store):
    """运行时防御：即使绕过启动门禁，limit<=0 也 fail-closed 不放行。"""
    store, rid = env_store
    with pytest.raises(ValueError, match="正整数"):
        revision.consume_budget(rid, "revision", 0)
    with pytest.raises(ValueError, match="正整数"):
        revision.consume_budget(rid, "revision", -2)
