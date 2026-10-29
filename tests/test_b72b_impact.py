# -*- coding: utf-8 -*-
"""B7-2b 连带影响核心测试（spec-b7-2-impact.md §三 + 开工令四验收锚点）。

锚点对照：
- 检索检查条款正文（词族命中带原文摘录，非孤立标题词）→ test_*clause_body*
- 分析用户选中的草案版本（未选定/选别版拒绝，选中版文本进提示词）→ test_*selected*
- 未覆盖如实标（不装覆盖、不写「没有冲突」、不调 LLM 不扣额度）→ test_*uncovered*
- 预算持久累计（3 次拒绝 + emit_skip + 跨进程持久）→ test_*budget*
全程 mock，零 LLM 花费。
"""
from __future__ import annotations

import json
import sqlite3
from unittest import mock

import pytest

from app.services import impact, revision
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

# 合同全文：定金条款正文在「第九条」正文里（锚点一：检索必须打到正文）
_PARSED_TEXT = (
    "第一条 合同标的：办公设备采购。"
    "第七条 付款方式：验收合格后一次性支付。"
    "第九条 定金：乙方违约的定金不予退还，并赔偿甲方损失。"
)

_FINDINGS_OK = json.dumps({"findings": [{"index": 0,
    "note": "该摘录与草案中定金罚则的约定存在不一致：赔偿口径不同"}]},
    ensure_ascii=False)


@pytest.fixture()
def env_store(tmp_path, monkeypatch):
    monkeypatch.setenv("STORE_DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.delenv("LLM_REVISION_BUDGET_PER_REVIEW", raising=False)
    monkeypatch.delenv("LLM_IMPACT_BUDGET_PER_REVIEW", raising=False)
    monkeypatch.setattr(impact.llm_ask, "_deepseek_key", lambda: "test-ds-key")
    store = ReviewStore(db_path=str(tmp_path / "t.db"))
    rid = store.create(filename="c.docx", category="procurement",
                       parsed_text=_PARSED_TEXT)
    store.update(rid, status="done", items=[
        {"id": "unfair_terms", "name": "公平性", "status": "需关注",
         "note": "定金单方没收", "quote": "乙方违约的定金不予退还。",
         "hits": ["不予退还"]},
    ])
    return store, rid


def _gen_ok(store, rid, item_id="unfair_terms"):
    r = revision.generate_revision(store, rid, item_id, llm_call=lambda *a: _GOOD)
    assert r["status"] == "ok"
    return r


def _select(store, rid, item_id="unfair_terms"):
    r = _gen_ok(store, rid, item_id)
    sel = impact.select_draft(store, rid, item_id, r["draft_id"])
    assert sel["status"] == "selected"
    return r["draft_id"]


def _impact_ok_call(observed: dict | None = None):
    def call(api_key: str, system: str, user: str) -> str:
        if observed is not None:
            observed["system"] = system
            observed["user"] = user
        return _FINDINGS_OK
    return call


# ---------- 锚点二：draft_id 全链可用（spec §3.1 硬要求） ----------

def test_generate_ok_response_carries_draft_id(env_store):
    store, rid = env_store
    r = _gen_ok(store, rid)
    assert r["draft_id"], "ok 响应必须携带 draft_id（选定/连带影响的回指标识）"
    d = revision.latest_draft(rid, "unfair_terms")
    assert d is not None and d["status"] == "ok"


# ---------- 选定：归属校验（跨审查单/跨 item/降级草案拒绝） ----------

def test_select_rejects_cross_review_draft_id(env_store):
    store, rid = env_store
    r = _gen_ok(store, rid)
    # 另一份审查单拿同一 draft_id 来选定：归属不符必须拒绝
    rid2 = store.create(filename="other.docx", category="procurement",
                        parsed_text=_PARSED_TEXT)
    store.update(rid2, status="done", items=[
        {"id": "unfair_terms", "name": "公平性", "status": "需关注",
         "note": "n", "quote": "q", "hits": []}])
    sel = impact.select_draft(store, rid2, "unfair_terms", r["draft_id"])
    assert sel == {"status": "not_found", "reason": "draft_not_found"}


def test_select_rejects_cross_item_draft_id(env_store):
    store, rid = env_store
    r = _gen_ok(store, rid)
    sel = impact.select_draft(store, rid, "term", r["draft_id"])
    assert sel["status"] == "not_found", "跨 item 的 draft_id 必须拒绝"


def test_select_rejects_degraded_draft(env_store):
    store, rid = env_store
    with mock.patch.object(revision, "_parse_versions",
                           side_effect=revision._Degraded("incomplete")):
        r = revision.generate_revision(store, rid, "unfair_terms",
                                       llm_call=lambda *a: _GOOD)
    assert r["status"] == "degraded"
    # 降级响应不携带 draft_id（无可选对象）；直查库取降级草稿 id 来试选定
    con = sqlite3.connect(str(store._path))
    row = con.execute("SELECT id FROM revision_drafts WHERE review_id=?"
                      " AND status='degraded'", (rid,)).fetchone()
    con.close()
    sel = impact.select_draft(store, rid, "unfair_terms", row[0])
    assert sel["status"] == "not_supported"
    assert sel["reason"] == "draft_degraded"


def test_select_replaces_previous_choice(env_store):
    """重新选定=改主意：同一 (review, item) 只保留最新选定（主键幂等）。"""
    store, rid = env_store
    d1 = _select(store, rid)
    d2 = _select(store, rid)
    assert d1 != d2
    assert impact.get_selected_draft_id(rid, "unfair_terms") == d2


# ---------- 锚点三：impact 只分析「用户选中的那一版」 ----------

def test_impact_requires_selection(env_store):
    store, rid = env_store
    r = _gen_ok(store, rid)
    llm = mock.Mock()
    res = impact.analyze_impact(store, rid, "unfair_terms", r["draft_id"],
                                llm_call=llm)
    assert res["status"] == "not_supported"
    assert res["reason"] == "draft_not_selected"
    llm.assert_not_called(), "未选定就 impact=拒绝，零 LLM 调用"


def test_impact_rejects_mismatched_draft_id(env_store):
    """选了 A 版却拿 B 版的 draft_id 来分析：拒绝（不迁就、不偷换分析对象）。"""
    store, rid = env_store
    _select(store, rid)  # 选定第一版
    r2 = _gen_ok(store, rid)  # 又生成一版（未选定）
    llm = mock.Mock()
    res = impact.analyze_impact(store, rid, "unfair_terms", r2["draft_id"],
                                llm_call=llm)
    assert res["status"] == "not_supported"
    assert res["reason"] == "draft_mismatch"
    llm.assert_not_called()


def test_impact_analyzes_selected_draft_text(env_store):
    """分析的是选中版草案文本（进提示词的必须是那一版，不是原文）。"""
    store, rid = env_store
    observed: dict = {}
    draft_id = _select(store, rid)
    res = impact.analyze_impact(store, rid, "unfair_terms", draft_id,
                                llm_call=_impact_ok_call(observed))
    assert res["status"] == "ok"
    assert "改为对等罚则表述。" in observed["user"], \
        "提示词必须携带选中版草案文本（分析对象=选中版，spec §3.1）"
    # 锚点一：摘录来自合同全文条款正文（含「第九条 定金：…」正文而非孤立词）
    assert "定金不予退还" in observed["user"]
    assert res["report"]["selected_draft_id"] == draft_id


# ---------- 锚点四：未覆盖=诚实定位（不装覆盖、零 LLM、零额度） ----------

def test_uncovered_honest_state(env_store, monkeypatch):
    store, rid = env_store
    # 换一份全文：不含任何 unfair_terms 词族关键词
    store.update(rid, parsed_text="第一条 标的：设备采购。第二条 包装要求。")
    draft_id = _select(store, rid)
    llm = mock.Mock()
    res = impact.analyze_impact(store, rid, "unfair_terms", draft_id,
                                llm_call=llm)
    assert res["status"] == "uncovered"
    llm.assert_not_called(), "未覆盖不得调 LLM（定位失败≠分析）"
    rep = res["report"]
    assert rep["covered"] is False
    # 措辞纪律：未覆盖绝不能表述为「没有冲突」
    assert "不代表没有冲突" in rep["uncovered_reason"]
    assert "没有冲突" != rep["uncovered_reason"].strip()
    assert rep["notice"] == impact_prompts_notice(), "未覆盖也要带重审提示"
    # 未覆盖不扣额度
    assert revision.remaining_budget(rid, "impact", 3) == 3


def impact_prompts_notice():
    from app.prompts import impact_prompts
    return impact_prompts.RE_REVIEW_NOTICE


# ---------- 锚点五：预算持久累计（3 次拒绝 + emit_skip） ----------

def test_impact_budget_persists_and_emits_skip(env_store, monkeypatch):
    monkeypatch.setenv("LLM_IMPACT_BUDGET_PER_REVIEW", "2")
    store, rid = env_store
    draft_id = _select(store, rid)
    for i in range(2):
        res = impact.analyze_impact(store, rid, "unfair_terms", draft_id,
                                    llm_call=_impact_ok_call())
        assert res["status"] == "ok", f"第 {i+1} 次应成功"
    with mock.patch.object(impact.llm_call_log, "emit_skip") as skip:
        res = impact.analyze_impact(store, rid, "unfair_terms", draft_id,
                                    llm_call=_impact_ok_call())
    assert res["status"] == "budget_exceeded"
    skip.assert_called_once(), "预算拒绝必须 emit_skip 显式落账"
    # 额度持久落库（重启/跨请求不重领）
    con = sqlite3.connect(str(store._path))
    row = con.execute("SELECT impact_used FROM revision_budget"
                      " WHERE review_id=?", (rid,)).fetchone()
    con.close()
    assert row == (2,)


# ---------- 报告纪律：重审提示 + Schema 校验 ----------

def test_ok_report_carries_re_review_notice(env_store):
    store, rid = env_store
    draft_id = _select(store, rid)
    res = impact.analyze_impact(store, rid, "unfair_terms", draft_id,
                                llm_call=_impact_ok_call())
    assert res["status"] == "ok"
    assert res["report"]["notice"] == impact_prompts_notice()
    assert res["trace_id"], "每次 LLM 调用必须有 trace_id"


def test_findings_index_out_of_range_degrades(env_store):
    store, rid = env_store
    draft_id = _select(store, rid)
    bad = json.dumps({"findings": [{"index": 99, "note": "越界"}]},
                     ensure_ascii=False)
    res = impact.analyze_impact(store, rid, "unfair_terms", draft_id,
                                llm_call=lambda *a: bad)
    assert res["status"] == "degraded" and res["reason"] == "incomplete"


def test_findings_empty_note_degrades(env_store):
    store, rid = env_store
    draft_id = _select(store, rid)
    bad = json.dumps({"findings": [{"index": 0, "note": "  "}]},
                     ensure_ascii=False)
    res = impact.analyze_impact(store, rid, "unfair_terms", draft_id,
                                llm_call=lambda *a: bad)
    assert res["status"] == "degraded" and res["reason"] == "incomplete"


def test_findings_empty_array_is_valid(env_store):
    """模型如实说「无不一致」：findings 空数组=合法结果（非降级）。"""
    store, rid = env_store
    draft_id = _select(store, rid)
    res = impact.analyze_impact(
        store, rid, "unfair_terms", draft_id,
        llm_call=lambda *a: json.dumps({"findings": []}, ensure_ascii=False))
    assert res["status"] == "ok"
    assert res["report"]["findings"] == []
    assert res["report"]["covered"] is True


# ---------- 锚点一：检索打到条款正文（非孤立标题词） ----------

def test_find_related_clauses_hits_clause_body():
    exc = impact.find_related_clauses(_PARSED_TEXT, "unfair_terms")
    assert exc, "词族必须命中正文里的『定金』"
    joined = " ".join(e["excerpt"] for e in exc)
    assert "定金不予退还" in joined, "摘录必须是条款正文窗口而非孤立词"
    assert all(e["position"] >= 0 for e in exc)
    assert [e["index"] for e in exc] == list(range(len(exc))), "index 连续编号"


def test_find_related_clauses_unknown_item_no_default_family():
    """无配置词族的 item 不做泛化检索（fail-closed：宁可未覆盖不乱扫）。"""
    assert impact.find_related_clauses(_PARSED_TEXT, "no_such_item") == []


def test_families_yaml_fail_closed(tmp_path, monkeypatch):
    """词族配置非法必须 fail-closed 抛错（对齐清单配置纪律）。"""
    bad = tmp_path / "bad.yaml"
    bad.write_text("version: 1\nfamilies:\n  unfair_terms: []\n",
                   encoding="utf-8")
    monkeypatch.setattr(impact, "_KEYWORDS_PATH", bad)
    impact._load_families.cache_clear()
    with pytest.raises(ValueError, match="非法"):
        impact._load_families()
    impact._load_families.cache_clear()


# ---------- 清理：选定记录/影响报告随审查单过期同事务清理 ----------

def test_purge_carries_selection_and_reports(env_store):
    store, rid = env_store
    draft_id = _select(store, rid)
    impact.analyze_impact(store, rid, "unfair_terms", draft_id,
                          llm_call=_impact_ok_call())
    # 强制过期（同 B7-1 手法），走真实 get 删除路径
    con = sqlite3.connect(str(store._path))
    con.execute("UPDATE reviews SET created_at = ? WHERE id = ?",
                (__import__("time").time() - 10**9, rid))
    con.commit()
    con.close()
    assert store.get(rid) is None
    con = sqlite3.connect(str(store._path))
    sel = con.execute("SELECT COUNT(*) FROM selected_drafts"
                      " WHERE review_id=?", (rid,)).fetchone()[0]
    rep = con.execute("SELECT COUNT(*) FROM impact_reports"
                      " WHERE review_id=?", (rid,)).fetchone()[0]
    con.close()
    assert (sel, rep) == (0, 0), "审查单删除后选定记录/影响报告必须随之清理"
