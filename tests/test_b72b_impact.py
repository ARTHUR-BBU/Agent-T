# -*- coding: utf-8 -*-
"""B7-2b 连带影响核心测试（spec-b7-2-impact.md §三 + #154 复批四项修复）。

复批修复对照：
1. 正文读真实存储字段 `text`（parsed_text 是管道入参非落库字段）+
   从选定到分析的完整入口测试 → test_*full_entry* / fixture 形态
2. 选定落具体版别（一个 draft_id 装三版，分析对象=选中的那一版）
   → test_impact_uses_each_selected_stance
3. 排除被改条款自身（证据坐标+条款索引归属；仅原条款命中=如实未覆盖）
   → test_original_clause_only_is_uncovered / test_excludes_original_clause
4. findings 缺字段/空对象=降级（不得把「没给」当「没有」）
   → test_findings_missing_field_degrades / test_findings_empty_object_degrades
全程 mock，零 LLM 花费。
"""
from __future__ import annotations

import json
import sqlite3
import time
from unittest import mock

import pytest

from app.services import impact, revision
from app.services.clause_index import build_clause_index
from app.services.evidence import _clause_at
from app.services.store import ReviewStore

# 真实形态 fixture：正文含被改条款（第九条 定金）+ 其他引用条款
# （第十二条 违约责任），词族在两边都命中——排除逻辑才有区分度
_C3_QUOTE = "乙方违约的定金不予退还"
_TEXT = (
    "第一条 合同标的：办公设备采购。\n"
    "第七条 付款方式：验收合格后一次性支付。\n"
    f"第九条 定金：{_C3_QUOTE}。\n"
    "第十二条 违约责任：违约方应赔偿守约方损失。\n"
)
_CLAUSE_INDEX = build_clause_index(_TEXT)
_EV_START = _TEXT.find(_C3_QUOTE)
_EV_END = _EV_START + len(_C3_QUOTE)
_ORIGINAL_CLAUSE_ID = _clause_at(_CLAUSE_INDEX, _EV_START)

_GOOD = json.dumps({
    "versions": [
        {"stance": "稳妥", "text": "改为对等罚则表述。", "note": "最容易谈成"},
        {"stance": "平衡", "text": "折中方案文本。", "note": "双方各让一步"},
        {"stance": "进取", "text": "删除没收约定并加倍返还。", "note": "我方强势时用"},
    ],
    "rationale": "三版差异说明。",
    "assumptions": ["假设定金给付方为乙方"],
}, ensure_ascii=False)

_FINDINGS_OK = json.dumps({"findings": [{"index": 0,
    "note": "该摘录与草案中定金罚则的约定存在不一致：赔偿口径不同"}]},
    ensure_ascii=False)


def _item() -> dict:
    return {"id": "unfair_terms", "name": "公平性", "status": "需关注",
            "note": "定金单方没收", "quote": _C3_QUOTE + "。",
            "hits": ["不予退还"],
            "evidence_start": _EV_START, "evidence_end": _EV_END,
            "primary_clause_id": _ORIGINAL_CLAUSE_ID or "",
            "clause_ids": [_ORIGINAL_CLAUSE_ID] if _ORIGINAL_CLAUSE_ID else []}


@pytest.fixture()
def env_store(tmp_path, monkeypatch):
    """真实审查单形态：正文存 `text`、条款索引存 `clause_index`。"""
    monkeypatch.setenv("STORE_DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.delenv("LLM_REVISION_BUDGET_PER_REVIEW", raising=False)
    monkeypatch.delenv("LLM_IMPACT_BUDGET_PER_REVIEW", raising=False)
    monkeypatch.setattr(impact.llm_ask, "_deepseek_key", lambda: "test-ds-key")
    store = ReviewStore(db_path=str(tmp_path / "t.db"))
    rid = store.create(filename="c.docx", category="procurement")
    store.update(rid, status="done", items=[_item()], text=_TEXT,
                 clause_index=_CLAUSE_INDEX)
    return store, rid


def _gen_ok(store, rid, item_id="unfair_terms"):
    r = revision.generate_revision(store, rid, item_id, llm_call=lambda *a: _GOOD)
    assert r["status"] == "ok"
    return r


def _select(store, rid, stance="平衡", item_id="unfair_terms"):
    r = _gen_ok(store, rid, item_id)
    sel = impact.select_draft(store, rid, item_id, r["draft_id"], stance)
    assert sel["status"] == "selected", sel
    return r["draft_id"]


def _impact_ok_call(observed: dict | None = None):
    def call(api_key: str, system: str, user: str) -> str:
        if observed is not None:
            observed["system"] = system
            observed["user"] = user
        return _FINDINGS_OK
    return call


# ---------- draft_id 全链可用（spec §3.1 硬要求） ----------

def test_generate_ok_response_carries_draft_id(env_store):
    store, rid = env_store
    r = _gen_ok(store, rid)
    assert r["draft_id"], "ok 响应必须携带 draft_id（选定/连带影响的回指标识）"


# ---------- 选定：归属+版别校验 ----------

def test_select_requires_stance(env_store):
    store, rid = env_store
    r = _gen_ok(store, rid)
    sel = impact.select_draft(store, rid, "unfair_terms", r["draft_id"], "")
    assert sel["status"] == "not_supported"
    assert sel["reason"] == "stance_invalid", "空版别必须拒绝（选定必须落到版别）"


def test_select_rejects_unknown_stance(env_store):
    store, rid = env_store
    r = _gen_ok(store, rid)
    sel = impact.select_draft(store, rid, "unfair_terms", r["draft_id"], "激进")
    assert sel["status"] == "not_supported"
    assert sel["reason"] == "stance_invalid"


def test_select_rejects_cross_review_draft_id(env_store):
    store, rid = env_store
    r = _gen_ok(store, rid)
    rid2 = store.create(filename="other.docx", category="procurement")
    store.update(rid2, status="done", items=[_item()], text=_TEXT,
                 clause_index=_CLAUSE_INDEX)
    sel = impact.select_draft(store, rid2, "unfair_terms", r["draft_id"], "平衡")
    assert sel == {"status": "not_found", "reason": "draft_not_found"}


def test_select_rejects_cross_item_draft_id(env_store):
    store, rid = env_store
    r = _gen_ok(store, rid)
    sel = impact.select_draft(store, rid, "term", r["draft_id"], "平衡")
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
    sel = impact.select_draft(store, rid, "unfair_terms", row[0], "平衡")
    assert sel["status"] == "not_supported"
    assert sel["reason"] == "draft_degraded"


def test_select_replaces_previous_choice(env_store):
    """重新选定=改主意：同一 (review, item) 只保留最新选定（主键幂等）。"""
    store, rid = env_store
    d1 = _select(store, rid, stance="稳妥")
    d2 = _select(store, rid, stance="进取")
    assert d1 != d2
    assert impact.get_selected(rid, "unfair_terms") == (d2, "进取")


# ---------- 复批 2：分析对象=选中的具体版别（三版分别验证） ----------

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
    store, rid = env_store
    _select(store, rid)
    r2 = _gen_ok(store, rid)
    llm = mock.Mock()
    res = impact.analyze_impact(store, rid, "unfair_terms", r2["draft_id"],
                                llm_call=llm)
    assert res["status"] == "not_supported"
    assert res["reason"] == "draft_mismatch"
    llm.assert_not_called()


def test_impact_uses_each_selected_stance(env_store):
    """三版分别选定，送去分析的必须是该版文本（非固定第一版）。"""
    store, rid = env_store
    observed: dict = {}
    for stance in ("稳妥", "平衡", "进取"):
        draft_id = _select(store, rid, stance=stance)
        res = impact.analyze_impact(store, rid, "unfair_terms", draft_id,
                                    llm_call=_impact_ok_call(observed))
        assert res["status"] == "ok", (stance, res)
        assert res["report"]["selected_stance"] == stance
    want = {"稳妥": "改为对等罚则表述。", "平衡": "折中方案文本。",
            "进取": "删除没收约定并加倍返还。"}
    # 每次分析用的是当次选中版：最后一次=进取版文本在提示词里
    assert want["进取"] in observed["user"]
    assert "选定草案（改写后的条款文本）" in observed["user"]


def test_impact_analyzes_selected_draft_and_contract_body(env_store):
    """复批 1：读真实 text 字段——完整链路 generate→select→impact 全绿；
    摘录来自条款正文（非假「未覆盖」）。"""
    store, rid = env_store
    observed: dict = {}
    draft_id = _select(store, rid)
    res = impact.analyze_impact(store, rid, "unfair_terms", draft_id,
                                llm_call=_impact_ok_call(observed))
    assert res["status"] == "ok", f"真实形态审查单必须能定位到引用条款：{res}"
    assert res["report"]["selected_draft_id"] == draft_id
    assert "折中方案文本。" in observed["user"], "提示词必须携带选中版草案文本"
    # 复批 3：摘录必须是其他条款（第十二条），不是被改的第九条自己
    for e in res["report"]["excerpts"]:
        assert "定金不予退还" not in e["excerpt"], \
            "摘录不得是被改条款自身"
    assert any("违约责任" in e["excerpt"] for e in res["report"]["excerpts"]), \
        "第十二条引用条款必须被检索到"


# ---------- 复批 3：原条款排除边界 ----------

def test_original_clause_only_is_uncovered(env_store):
    """合同里只有被改条款自身命中词族：不得报「已覆盖」，如实标未覆盖。"""
    store, rid = env_store
    only_original = f"第九条 定金：{_C3_QUOTE}，并赔偿甲方损失。"
    idx = build_clause_index(only_original)
    ev = only_original.find(_C3_QUOTE)
    item = _item()
    item["evidence_start"], item["evidence_end"] = ev, ev + len(_C3_QUOTE)
    item["primary_clause_id"] = _clause_at(idx, ev) or ""
    item["clause_ids"] = [item["primary_clause_id"]] if item["primary_clause_id"] else []
    store.update(rid, text=only_original, clause_index=idx, items=[item])
    draft_id = _select(store, rid)
    llm = mock.Mock()
    res = impact.analyze_impact(store, rid, "unfair_terms", draft_id,
                                llm_call=llm)
    assert res["status"] == "uncovered", "仅原条款命中=未找到其他相关条款"
    llm.assert_not_called(), "未覆盖不调 LLM"
    rep = res["report"]
    assert rep["covered"] is False
    assert "不代表没有冲突" in rep["uncovered_reason"], "措辞纪律：禁写「没有冲突」"
    # 未覆盖不扣额度
    assert revision.remaining_budget(rid, "impact", 3) == 3


def test_excerpt_clips_full_original_clause_span(env_store):
    """复批 P2 追加：引句只覆盖原条款一部分时，引句之后的条款尾巴
    同样不得渗进其他条款摘录——按条款索引完整 span 裁剪（右开区间）。"""
    store, rid = env_store
    text = ("第九条 定金：乙方违约定金不予退还；原条款尾部独有内容。\n"
            "第十二条 违约责任：违约方应赔偿守约方损失。")
    idx = build_clause_index(text)
    q = "乙方违约定金不予退还"  # 只覆盖第九条的一部分
    ev = text.find(q)
    cid = _clause_at(idx, ev)
    item = _item()
    item.update(quote=q, evidence_start=ev, evidence_end=ev + len(q),
                primary_clause_id=cid or "",
                clause_ids=[cid] if cid else [])
    store.update(rid, text=text, clause_index=idx, items=[item])
    draft_id = _select(store, rid)
    observed: dict = {}
    res = impact.analyze_impact(store, rid, "unfair_terms", draft_id,
                                llm_call=_impact_ok_call(observed))
    assert res["status"] == "ok", res
    for e in res["report"]["excerpts"]:
        assert "原条款尾部独有内容" not in e["excerpt"], \
            f"被改条款的尾巴不得渗入摘录：{e['excerpt']}"
        assert "定金不予退还" not in e["excerpt"], \
            f"被改条款引句部分也不得渗入：{e['excerpt']}"
    assert any("违约责任" in e["excerpt"] and "赔偿守约方损失" in e["excerpt"]
               for e in res["report"]["excerpts"]), "关联条款原文必须完整保留"


def test_missing_text_is_uncovered(env_store):
    """审查单缺正文：诚实未覆盖（缺料≠没有冲突），不调 LLM。"""
    store, rid = env_store
    store.update(rid, text="")
    draft_id = _select(store, rid)
    llm = mock.Mock()
    res = impact.analyze_impact(store, rid, "unfair_terms", draft_id,
                                llm_call=llm)
    assert res["status"] == "uncovered"
    llm.assert_not_called()
    assert "不代表没有冲突" in res["report"]["uncovered_reason"]


# ---------- 预算持久累计（3 次拒绝 + emit_skip） ----------

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


# ---------- 报告纪律：重审提示 + Schema 校验（复批 4） ----------

def test_ok_report_carries_re_review_notice(env_store):
    store, rid = env_store
    draft_id = _select(store, rid)
    res = impact.analyze_impact(store, rid, "unfair_terms", draft_id,
                                llm_call=_impact_ok_call())
    assert res["status"] == "ok"
    from app.prompts import impact_prompts
    assert res["report"]["notice"] == impact_prompts.RE_REVIEW_NOTICE
    assert res["trace_id"], "每次 LLM 调用必须有 trace_id"


def test_findings_empty_object_degrades(env_store):
    """复批 4：模型回 {} = 未完成分析，必须降级（不得当「没有发现」）。"""
    store, rid = env_store
    draft_id = _select(store, rid)
    res = impact.analyze_impact(store, rid, "unfair_terms", draft_id,
                                llm_call=lambda *a: "{}")
    assert res["status"] == "degraded" and res["reason"] == "incomplete"


def test_findings_missing_field_degrades(env_store):
    store, rid = env_store
    draft_id = _select(store, rid)
    res = impact.analyze_impact(
        store, rid, "unfair_terms", draft_id,
        llm_call=lambda *a: json.dumps({"result": "ok"}, ensure_ascii=False))
    assert res["status"] == "degraded" and res["reason"] == "incomplete"


def test_findings_null_degrades(env_store):
    store, rid = env_store
    draft_id = _select(store, rid)
    res = impact.analyze_impact(store, rid, "unfair_terms", draft_id,
                                llm_call=lambda *a: "null")
    assert res["status"] == "degraded" and res["reason"] == "incomplete"


def test_findings_explicit_empty_array_is_valid(env_store):
    """仅显式 [] = 合法的「无不一致」（非降级）。"""
    store, rid = env_store
    draft_id = _select(store, rid)
    res = impact.analyze_impact(
        store, rid, "unfair_terms", draft_id,
        llm_call=lambda *a: json.dumps({"findings": []}, ensure_ascii=False))
    assert res["status"] == "ok"
    assert res["report"]["findings"] == []
    assert res["report"]["covered"] is True


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


# ---------- 检索单元：词族命中条款正文 + 配置 fail-closed ----------

def test_find_related_clauses_hits_clause_body_and_excludes_original():
    ranges = [(_EV_START, _EV_END + 1)]
    exc = impact.find_related_clauses(_TEXT, "unfair_terms",
                                      exclude_positions=ranges)
    assert exc, "词族必须命中其他条款正文（第十二条）"
    joined = " ".join(e["excerpt"] for e in exc)
    assert "违约责任" in joined or "赔偿" in joined, "摘录须为引用条款正文窗口"
    assert "定金不予退还" not in joined, "被改条款自身必须被排除"
    assert all(e["position"] >= 0 for e in exc)
    assert [e["index"] for e in exc] == list(range(len(exc))), "index 连续编号"


def test_find_related_clauses_without_exclusion_includes_original():
    """对照：不排除时原条款也会命中（证明排除逻辑在起作用）。"""
    exc = impact.find_related_clauses(_TEXT, "unfair_terms")
    assert any("定金不予退还" in e["excerpt"] for e in exc)


def test_find_related_clauses_unknown_item_no_default_family():
    """无配置词族的 item 不做泛化检索（fail-closed：宁可未覆盖不乱扫）。"""
    assert impact.find_related_clauses(_TEXT, "no_such_item") == []


def test_families_yaml_fail_closed(tmp_path, monkeypatch):
    bad = tmp_path / "bad.yaml"
    bad.write_text("version: 1\nfamilies:\n  unfair_terms: []\n",
                   encoding="utf-8")
    monkeypatch.setattr(impact, "_KEYWORDS_PATH", bad)
    impact._load_families.cache_clear()
    with pytest.raises(ValueError, match="非法"):
        impact._load_families()
    impact._load_families.cache_clear()


# ---------- HTTP 全入口：generate → select → impact 走真实路由 ----------

def test_full_entry_via_routes(env_store, monkeypatch):
    """复批 1 补测：从选定草案到分析的完整入口走 HTTP 路由（真实形态）。"""
    from app.api import routes_revision
    from app.main import app
    from fastapi.testclient import TestClient
    store, rid = env_store
    monkeypatch.setattr(routes_revision, "store", store)
    # 路由层走默认 LLM 通道：patch 成 mock 回执（严禁真调用）
    monkeypatch.setattr(revision, "_default_llm_call", lambda *a: _GOOD)
    monkeypatch.setattr(impact, "_default_llm_call", lambda *a: _FINDINGS_OK)
    client = TestClient(app)

    r1 = client.post(f"/api/review/{rid}/items/unfair_terms/revision")
    assert r1.status_code == 200, r1.text
    draft_id = r1.json()["draft_id"]

    # 未选定就 impact → 422
    r2 = client.post(f"/api/review/{rid}/items/unfair_terms/revision/impact",
                     json={"draft_id": draft_id})
    assert r2.status_code == 422 and r2.json()["detail"] == "draft_not_selected"

    # 缺版别选定 → 422 stance_invalid
    r3 = client.post(f"/api/review/{rid}/items/unfair_terms/revision/select",
                     json={"draft_id": draft_id})
    assert r3.status_code == 422 and r3.json()["detail"] == "stance_invalid"

    # 选定平衡版 → 200
    r4 = client.post(f"/api/review/{rid}/items/unfair_terms/revision/select",
                     json={"draft_id": draft_id, "stance": "平衡"})
    assert r4.status_code == 200 and r4.json() == {
        "status": "selected", "draft_id": draft_id, "stance": "平衡"}

    # impact 分析选中版 → 200 ok
    r5 = client.post(f"/api/review/{rid}/items/unfair_terms/revision/impact",
                     json={"draft_id": draft_id})
    assert r5.status_code == 200, r5.text
    body = r5.json()
    assert body["status"] == "ok"
    assert body["report"]["selected_stance"] == "平衡"
    assert body["report"]["notice"]


# ---------- 清理：选定记录/影响报告随审查单过期同事务清理 ----------

def test_purge_carries_selection_and_reports(env_store):
    store, rid = env_store
    draft_id = _select(store, rid)
    impact.analyze_impact(store, rid, "unfair_terms", draft_id,
                          llm_call=_impact_ok_call())
    # 强制过期，走真实 get 删除路径
    con = sqlite3.connect(str(store._path))
    con.execute("UPDATE reviews SET created_at = ? WHERE id = ?",
                (time.time() - 10**9, rid))
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
