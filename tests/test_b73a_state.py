# -*- coding: utf-8 -*-
"""B7-3a 聚合状态接口测试（spec-b7-3-ui-report.md §四A + 线框 v2）。

覆盖面：
- 纯读库红线：state 读取零 AI 调用、零扣额（LLM 通道 patch 成即炸）
- drafts 倒序 + 全文恢复字段（versions/rationale/assumptions/declaration）
- 选定态（is_selected/selected_stance/selected {draft_id, stance}）
- 牵连最新一次：ok（绑定版别+stale）/ uncovered（no_match vs missing_source
  机器可读细分）/ degraded / legacy（老行 stance 为空）
- latest-one 语义：两条记录只回最新一条，不捞旧成功充数
- findings 输出检查：禁语说明隐藏+计数；摘录（合同原文）不过滤
- 幂等迁移：老表（无 stance 列）自动补列
- 路由层：404 审查单/检查项，200 完整形态
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


def _ensure_tables(store):
    """直插前确保两表存在（rev/impact 各自懒建表；直插场景先触发建表）。"""
    revision._connect().close()
    with impact._connect() as conn:
        conn.execute(impact._IMPACT_REPORTS_DDL)


def _insert_impact_row(store, rid, *, draft_id="d0", stance="", status="ok",
                       payload="{}", reason="", created=None, item_id="unfair_terms"):
    """直接落一条 impact_reports（绕过 analyze，便于构造 legacy/细分态）。"""
    _ensure_tables(store)
    con = sqlite3.connect(str(store._path))
    con.execute(
        "INSERT INTO impact_reports (id, review_id, item_id, draft_id,"
        " created_at, trace_id, payload, status, reason, stance)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (f"ir-{created}", rid, item_id, draft_id,
         created if created is not None else time.time(), "trace-x",
         payload, status, reason, stance))
    con.commit()
    con.close()


# ---------- 纯读库红线 ----------

def test_state_is_pure_read_no_llm_no_charge(env_store):
    """读取 state 不许碰任何 LLM 通道、不许扣额度（施工卡 §四A）。"""
    store, rid = env_store
    draft_id = _select(store, rid)
    impact.analyze_impact(store, rid, "unfair_terms", draft_id,
                          llm_call=lambda *a: _FINDINGS_OK)

    def _explode(*a, **k):
        raise AssertionError("state 读取禁止触发 AI 调用")
    with mock.patch.object(revision, "_default_llm_call", _explode), \
         mock.patch.object(impact, "_default_llm_call", _explode):
        s1 = impact.revision_state(store, rid, "unfair_terms")
    assert s1["status"] == "ok"
    s2 = impact.revision_state(store, rid, "unfair_terms")
    assert s1 == s2, "纯读接口两次读取必须逐字段一致"


# ---------- 空态 / drafts 全文恢复 ----------

def test_state_empty(env_store):
    store, rid = env_store
    s = impact.revision_state(store, rid, "unfair_terms")
    assert s["status"] == "ok"
    assert s["drafts"] == []
    assert s["selected"] is None
    assert s["impact"] is None
    assert s["remaining"]["revision"] > 0
    assert s["remaining"]["impact"] > 0


def test_state_restores_draft_full_fields(env_store):
    store, rid = env_store
    r = _gen_ok(store, rid)
    s = impact.revision_state(store, rid, "unfair_terms")
    assert len(s["drafts"]) == 1
    d = s["drafts"][0]
    assert d["draft_id"] == r["draft_id"]
    assert d["status"] == "ok"
    assert d["is_selected"] is False
    assert [v["stance"] for v in d["versions"]] == ["稳妥", "平衡", "进取"]
    assert d["versions"][0]["text"] == "改为对等罚则表述。"
    assert d["rationale"] == "三版差异说明。"
    assert d["assumptions"] == ["假设定金给付方为乙方"]
    # 起草辅助免责声明上游必注入（保存时统一落 payload），不得缺失
    assert "不构成法律意见" in d["declaration"]


def test_state_reflects_selection(env_store):
    store, rid = env_store
    draft_id = _select(store, rid, stance="平衡")
    s = impact.revision_state(store, rid, "unfair_terms")
    assert s["selected"] == {"draft_id": draft_id, "stance": "平衡"}
    d = s["drafts"][0]
    assert d["is_selected"] is True
    assert d["selected_stance"] == "平衡"


def test_state_drafts_desc_and_latest_selected_marker(env_store):
    """两批草案倒序排列；选定标在「那批」上（前端 ● 角标数据源）。"""
    store, rid = env_store
    _ensure_tables(store)
    con = sqlite3.connect(str(store._path))
    old_t, new_t = time.time() - 100, time.time()
    for did, t in (("d-old", old_t), ("d-new", new_t)):
        con.execute(
            "INSERT INTO revision_drafts (id, review_id, item_id, created_at,"
            " status, payload) VALUES (?,?,?,?,?,?)",
            (did, rid, "unfair_terms", t, "ok", _GOOD))
    con.commit()
    con.close()
    impact.select_draft(store, rid, "unfair_terms", "d-old", "平衡")
    s = impact.revision_state(store, rid, "unfair_terms")
    assert [d["draft_id"] for d in s["drafts"]] == ["d-new", "d-old"]
    assert s["drafts"][0]["is_selected"] is False
    assert s["drafts"][1]["is_selected"] is True
    assert s["selected"] == {"draft_id": "d-old", "stance": "平衡"}


def test_state_broken_draft_payload_degrades(env_store):
    """库里 payload 坏了 → 该批如实 degraded，不展示残缺方案。"""
    store, rid = env_store
    _ensure_tables(store)
    con = sqlite3.connect(str(store._path))
    con.execute(
        "INSERT INTO revision_drafts (id, review_id, item_id, created_at,"
        " status, payload) VALUES (?,?,?,?,?,?)",
        ("d-bad", rid, "unfair_terms", time.time(), "ok", "{not-json"))
    con.commit()
    con.close()
    s = impact.revision_state(store, rid, "unfair_terms")
    assert s["drafts"][0]["status"] == "degraded"
    assert "versions" not in s["drafts"][0]


# ---------- 牵连态：ok / stale / legacy / uncovered 细分 / latest-one ----------

def test_state_impact_ok_bound_and_not_stale(env_store):
    store, rid = env_store
    draft_id = _select(store, rid)
    impact.analyze_impact(store, rid, "unfair_terms", draft_id,
                          llm_call=lambda *a: _FINDINGS_OK)
    s = impact.revision_state(store, rid, "unfair_terms")
    imp = s["impact"]
    assert imp["state"] == "ok"
    assert imp["bound"] == {"draft_id": draft_id, "stance": "平衡"}
    assert imp["legacy"] is False
    assert imp["stale"] is False
    assert imp["findings"] and imp["findings"][0]["index"] == 0
    assert imp["hidden_findings"] == 0
    assert "excerpts" in imp


def test_state_impact_stale_after_reselect(env_store):
    """选定换版后，旧分析绑定不变 → stale=True（前端按「已过期」渲染）。"""
    store, rid = env_store
    r1 = _gen_ok(store, rid)
    impact.select_draft(store, rid, "unfair_terms", r1["draft_id"], "平衡")
    impact.analyze_impact(store, rid, "unfair_terms", r1["draft_id"],
                          llm_call=lambda *a: _FINDINGS_OK)
    r2 = _gen_ok(store, rid)
    impact.select_draft(store, rid, "unfair_terms", r2["draft_id"], "进取")
    s = impact.revision_state(store, rid, "unfair_terms")
    imp = s["impact"]
    assert imp["bound"]["draft_id"] == r1["draft_id"]
    assert imp["stale"] is True
    assert s["selected"]["draft_id"] == r2["draft_id"]


def test_state_impact_legacy_missing_stance(env_store):
    """老行（stance 为空）→ legacy 标记；不硬配当前选定。"""
    store, rid = env_store
    _insert_impact_row(store, rid, draft_id="d0", stance="",
                       status="ok", payload=_FINDINGS_OK)
    s = impact.revision_state(store, rid, "unfair_terms")
    imp = s["impact"]
    assert imp["legacy"] is True
    assert imp["bound"]["stance"] is None
    assert imp["stale"] is True


def test_state_impact_latest_one_only(env_store):
    """两条记录只回最新一条；先成功后失败→如实显示失败，不捞旧成功充数。"""
    store, rid = env_store
    draft_id = _select(store, rid)
    impact.analyze_impact(store, rid, "unfair_terms", draft_id,
                          llm_call=lambda *a: _FINDINGS_OK)
    _insert_impact_row(store, rid, draft_id=draft_id, stance="平衡",
                       status="degraded", reason="llm_timeout",
                       payload="{}", created=time.time() + 10)
    s = impact.revision_state(store, rid, "unfair_terms")
    assert s["impact"]["state"] == "degraded"
    assert s["impact"]["reason"] == "llm_timeout"


def test_state_uncovered_code_no_match(env_store):
    store, rid = env_store
    _insert_impact_row(store, rid, draft_id="d0", stance="平衡",
                       status="uncovered",
                       payload=json.dumps({"uncovered_code": "no_match",
                                           "uncovered_reason": "未命中相关条款簇"},
                                          ensure_ascii=False))
    s = impact.revision_state(store, rid, "unfair_terms")
    imp = s["impact"]
    assert imp["state"] == "uncovered"
    assert imp["uncovered_code"] == "no_match"


def test_state_uncovered_code_missing_source(env_store):
    store, rid = env_store
    _insert_impact_row(store, rid, draft_id="d0", stance="平衡",
                       status="uncovered",
                       payload=json.dumps({"uncovered_reason": "合同缺少全文，"
                                           "无法定位可对照内容"},
                                          ensure_ascii=False))
    s = impact.revision_state(store, rid, "unfair_terms")
    assert s["impact"]["uncovered_code"] == "missing_source"


def test_uncovered_code_defaults_by_reason_text():
    """机器码缺失时按原因文案兜底：缺少/无法定位→missing_source，否则 no_match。"""
    assert impact._uncovered_code({"uncovered_code": "missing_source"}) == "missing_source"
    assert impact._uncovered_code({"uncovered_reason": "缺少全文"}) == "missing_source"
    assert impact._uncovered_code({"uncovered_reason": "无法定位原文"}) == "missing_source"
    assert impact._uncovered_code({"uncovered_reason": "其他部分未命中"}) == "no_match"
    assert impact._uncovered_code({}) == "no_match"


# ---------- findings 输出检查 ----------

def test_findings_output_check_hides_banned_notes():
    """禁语说明隐藏+计数；摘录（合同原文）原样不过滤。"""
    findings = [
        {"index": 0, "note": "口径不一致，建议核对"},
        {"index": 1, "note": "该条款建议签署前修改定金约定"},
        {"index": 2, "note": "与草案存在不一致"},
    ]
    visible, hidden = impact._findings_output_check(findings)
    assert hidden == 1
    assert [f["index"] for f in visible] == [0, 2]
    excerpts = [{"index": 1, "excerpt": _C3_QUOTE}]
    # 摘录不在 findings 检查范围——证据是合同原文，不是模型输出
    assert excerpts == [{"index": 1, "excerpt": _C3_QUOTE}]


def test_state_impact_reports_hidden_findings_count(env_store):
    # 直插构造（analyze 的 findings 校验要求下标与摘录一一对应，
    # 造「一条禁语+一条正常」不必走全链，state 组装逻辑是测试对象）
    payload = json.dumps({"findings": [
        {"index": 0, "note": "口径不一致"},
        {"index": 1, "note": "该条款建议接受，不必再改"},
    ]}, ensure_ascii=False)
    store, rid = env_store
    _insert_impact_row(store, rid, draft_id="d0", stance="平衡",
                       status="ok", payload=payload)
    s = impact.revision_state(store, rid, "unfair_terms")
    imp = s["impact"]
    assert imp["hidden_findings"] == 1
    assert [f["index"] for f in imp["findings"]] == [0]


# ---------- 幂等迁移 ----------

def test_migration_adds_stance_column_to_legacy_table(env_store):
    """老表（无 stance 列）自动补列：不报错、老行读为 legacy。"""
    store, rid = env_store
    con = sqlite3.connect(str(store._path))
    # 直建老形态表（无 stance 列）；revision_state 的 IF NOT EXISTS 不会覆盖它
    con.execute(
        "CREATE TABLE impact_reports ("
        "id TEXT PRIMARY KEY, review_id TEXT NOT NULL, item_id TEXT NOT NULL,"
        "draft_id TEXT NOT NULL, created_at REAL NOT NULL, trace_id TEXT,"
        "payload TEXT NOT NULL, status TEXT NOT NULL,"
        "reason TEXT NOT NULL DEFAULT '')")
    con.execute(
        "INSERT INTO impact_reports (id, review_id, item_id, draft_id,"
        " created_at, trace_id, payload, status, reason)"
        " VALUES ('ir-legacy', ?, 'unfair_terms', 'd0', ?, 't', ?, 'ok', '')",
        (rid, time.time(), _FINDINGS_OK))
    con.commit()
    con.close()
    s = impact.revision_state(store, rid, "unfair_terms")
    assert s["status"] == "ok"
    assert s["impact"]["legacy"] is True
    # 迁移幂等：再读不炸
    s2 = impact.revision_state(store, rid, "unfair_terms")
    assert s2["impact"]["legacy"] is True


# ---------- 404 与路由层 ----------

def test_state_not_found_review_and_item(env_store):
    store, rid = env_store
    assert impact.revision_state(store, "no-such", "unfair_terms") == {
        "status": "not_found", "reason": "review_not_found"}
    assert impact.revision_state(store, rid, "no-such-item") == {
        "status": "not_found", "reason": "item_not_found"}


def test_state_route_full_entry(env_store, monkeypatch):
    """GET state 路由：200 完整形态 + 404 映射（生成→选定→分析→读态全链）。"""
    from app.api import routes_revision
    from app.main import app
    from fastapi.testclient import TestClient
    store, rid = env_store
    monkeypatch.setattr(routes_revision, "store", store)
    monkeypatch.setattr(revision, "_default_llm_call", lambda *a: _GOOD)
    monkeypatch.setattr(impact, "_default_llm_call", lambda *a: _FINDINGS_OK)
    client = TestClient(app)

    # 404：审查单不存在
    assert client.get("/api/review/no-such/items/unfair_terms/revision/state"
                      ).status_code == 404
    # 404：检查项不存在
    assert client.get(f"/api/review/{rid}/items/no-such/revision/state"
                      ).status_code == 404

    # 空态：drafts 空 + selected 空
    r0 = client.get(f"/api/review/{rid}/items/unfair_terms/revision/state")
    assert r0.status_code == 200
    body0 = r0.json()
    assert body0["drafts"] == [] and body0["selected"] is None
    assert body0["impact"] is None

    # 生成→选定→分析
    r1 = client.post(f"/api/review/{rid}/items/unfair_terms/revision")
    assert r1.status_code == 200
    draft_id = r1.json()["draft_id"]
    r2 = client.post(f"/api/review/{rid}/items/unfair_terms/revision/select",
                     json={"draft_id": draft_id, "stance": "平衡"})
    assert r2.status_code == 200
    r3 = client.post(f"/api/review/{rid}/items/unfair_terms/revision/impact",
                     json={"draft_id": draft_id})
    assert r3.status_code == 200

    # 全态恢复
    r4 = client.get(f"/api/review/{rid}/items/unfair_terms/revision/state")
    assert r4.status_code == 200
    body = r4.json()
    assert body["selected"] == {"draft_id": draft_id, "stance": "平衡"}
    assert body["drafts"][0]["is_selected"] is True
    assert len(body["drafts"][0]["versions"]) == 3
    assert body["impact"]["state"] == "ok"
    assert body["impact"]["stale"] is False
    assert body["remaining"]["revision"] >= 0
    assert body["remaining"]["impact"] >= 0
