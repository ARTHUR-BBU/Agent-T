# -*- coding: utf-8 -*-
"""B7-5a「AI 发现」流水线合并测试（spec-b75a-ai-findings.md §六）。

覆盖：聚合合并（观察↔派生问题去重）、finding_key 防撞号（同标题
「待核实」+空引句不同问题必须不同号）、物化/幂等/反悔闭环（确认→
撤回→拒答资格→复活）、无坐标疑点不可物化、跨源「关联规则」标注、
派生词族、报告收录（观察类确认记录+范围文案新旧字节契约）。
全程零 LLM（spec：纯读/纯状态表/纯物化）。
"""
from __future__ import annotations

import io

import pytest
from docx import Document

from app.services import ai_findings as afs
from app.services.impact import derive_keywords_from_quote, find_related_clauses
from app.services.report import build_report_docx
from app.services.store import ReviewStore

# ---------------- 聚合视图 + finding_key ----------------


def _q(qid, title, question, *, source="pack_verify", quote="", clause_id="",
       status="pending"):
    return {"id": qid, "title": title, "question": question, "source": source,
            "quote": quote, "clause_id": clause_id, "status": status}


def test_finding_key_anti_collision_same_title_empty_quote():
    """codex #173 二轮实锤形态：同标题「待核实」+空引句的两条不同问题
    必须不同号（仅 title+quote 必撞）。"""
    row = {
        "id": "r", "quality": {"available": True, "observations": []},
        "verify": {"questions": [
            _q("a", "待核实", "问题A的完整正文"),
            _q("b", "待核实", "问题B的完整正文"),
        ]},
        "items": [],
    }
    keys = [f["finding_key"] for f in afs.review_findings(row)["findings"]]
    assert keys[0] != keys[1]
    # 同一问题两次聚合同号（稳定性）
    again = [f["finding_key"] for f in afs.review_findings(row)["findings"]]
    assert keys == again


def test_obs_key_same_title_different_clause():
    """观察同标题、不同条款 = 不同号（证据身份入键）。"""
    obs = [{"dimension": "completeness", "title": "缺验收",
            "quote": "同引句", "clause_id": "c01", "comment": ""}]
    row_a = {"id": "ra", "quality": {"available": True, "observations": obs},
             "verify": {"questions": []}, "items": []}
    row_b = {"id": "rb",
             "quality": {"available": True, "observations": [dict(obs[0],
                         clause_id="c02")]},
             "verify": {"questions": []}, "items": []}
    ka = afs.review_findings(row_a)["findings"][0]["finding_key"]
    kb = afs.review_findings(row_b)["findings"][0]["finding_key"]
    assert ka != kb


def test_merge_derived_question_into_observation():
    """派生问题（title 逐字沿用）并入观察为一条发现；独立疑点单独成条。"""
    row = {
        "id": "r",
        "quality": {"available": True, "observations": [
            {"dimension": "completeness", "title": "缺验收标准",
             "quote": "验收按双方口头", "clause_id": "c01", "comment": "c"},
        ]},
        "verify": {"questions": [
            _q("vq01", "缺验收标准", "请确认是否属实", source="quality",
               quote="验收按双方口头", clause_id="c01"),
            _q("vq09", "待核实", "独立疑点正文", source="pack_verify"),
        ]},
        "items": [],
    }
    fs = afs.review_findings(row)["findings"]
    assert len(fs) == 2  # 12 观察并入 8 派生只显示合并后的形态
    merged = fs[0]
    assert merged["source_type"] == "observ+question"
    assert merged["question_id"] == "vq01"
    assert merged["evidence"] is None  # 该问题行未带 verification
    standalone = fs[1]
    assert standalone["source_type"] == "standalone"
    assert standalone["question_id"] == "vq09"


def test_default_status_by_fact_type():
    """事实类默认待确认；观点类（impact）默认已阅（观点·无核对）。"""
    row = {
        "id": "r",
        "quality": {"available": True, "observations": [
            {"dimension": "completeness", "title": "t1", "quote": "q",
             "clause_id": "c01", "comment": ""},
            {"dimension": "impact", "title": "t2", "quote": "",
             "clause_id": "", "comment": ""},
        ]},
        "verify": {"questions": []},
        "items": [],
    }
    fs = afs.review_findings(row)["findings"]
    assert fs[0]["fact_type"] is True and fs[0]["status"] == "pending"
    assert fs[1]["fact_type"] is False and fs[1]["status"] == "viewed"


def test_rule_link_annotation():
    """同主题默认标「关联规则：{条目名}」；不同主题不标（不自动升互相印证）。"""
    row = {
        "id": "r",
        "quality": {"available": True, "observations": [
            {"dimension": "consistency", "title": "违约金约定与赔偿并用不明",
             "quote": "q", "clause_id": "c01", "comment": ""},
            {"dimension": "completeness", "title": "缺交付验收安排",
             "quote": "q2", "clause_id": "c02", "comment": ""},
        ]},
        "verify": {"questions": []},
        "items": [{"id": "penalty", "name": "违约金约定", "status": "需关注",
                   "note": "n", "quote": "x", "hits": []}],
    }
    fs = afs.review_findings(row)["findings"]
    assert fs[0]["rule_link"] == "关联规则：违约金约定"
    assert fs[1]["rule_link"] == ""

# ---------------- 物化 / 反悔闭环 ----------------


@pytest.fixture()
def env_store(tmp_path, monkeypatch):
    monkeypatch.setenv("STORE_DB_PATH", str(tmp_path / "t.db"))
    s = ReviewStore(db_path=str(tmp_path / "t.db"))
    rid = s.create(filename="c.txt", category="procurement")
    s.update(rid, status="done",
             quality={"available": True, "observations": [
                 {"dimension": "completeness", "title": "缺违约金上限",
                  "quote": "违约金为合同总价款的30%", "clause_id": "c02",
                  "comment": "无上限约定"}]},
             verify={"questions": [
                 _q("vq01", "缺违约金上限", "请确认是否属实", source="quality",
                    quote="违约金为合同总价款的30%", clause_id="c02")]},
             items=[{"id": "penalty", "name": "违约金约定", "status": "需关注",
                     "note": "n", "quote": "定金不予退还", "hits": []}])
    return s, rid


def _merged_key(store, rid):
    row = store.get(rid)
    return afs.review_findings(row)["findings"][0]["finding_key"]


def test_confirm_materializes_item_idempotent(env_store):
    s, rid = env_store
    key = _merged_key(s, rid)
    out = afs.set_status(s, rid, finding_key=key, status="confirmed")
    assert out["status"] == "ok" and out["materialized"]
    row = s.get(rid)
    ai_items = [i for i in row["items"] if i.get("tag_source") == "ai_finding"]
    assert len(ai_items) == 1
    it = ai_items[0]
    assert it["finding_key"] == key
    assert it["status"] == "需关注"
    assert it["name"].startswith("【AI 发现】")
    assert it["quote"] == "违约金为合同总价款的30%"
    assert it["primary_clause_id"] == "c02"
    # 幂等：重复确认不重复物化
    afs.set_status(s, rid, finding_key=key, status="confirmed")
    row = s.get(rid)
    assert len([i for i in row["items"]
                if i.get("tag_source") == "ai_finding"]) == 1


def test_dispute_revert_disables_and_reconfirm_revives(env_store):
    """反悔闭环：确认→物化→改争议→「需关注」资格失效→再确认复活。"""
    s, rid = env_store
    key = _merged_key(s, rid)
    afs.set_status(s, rid, finding_key=key, status="confirmed")
    out = afs.set_status(s, rid, finding_key=key, status="disputed")
    assert out["status"] == "ok"
    row = s.get(rid)
    it = next(i for i in row["items"] if i.get("tag_source") == "ai_finding")
    # 生成/选定/牵连/追问四入口的统一资格判据：status=="需关注"
    assert it["status"] == "已争议撤回"
    assert it["status"] != "需关注"  # 入口判据失效（历史留痕保留）
    # 再确认复活
    afs.set_status(s, rid, finding_key=key, status="confirmed")
    row = s.get(rid)
    it = next(i for i in row["items"] if i.get("tag_source") == "ai_finding")
    assert it["status"] == "需关注"
    assert len([i for i in row["items"]
                if i.get("tag_source") == "ai_finding"]) == 1


def test_set_status_never_touches_verify_questions(env_store):
    """服务职责边界：派生问题的 verify 状态由 confirm 链（路由层）负责，
    本服务只做物化——verify.questions 不被本函数改写。"""
    s, rid = env_store
    key = _merged_key(s, rid)
    afs.set_status(s, rid, finding_key=key, status="confirmed")
    row = s.get(rid)
    assert row["verify"]["questions"][0]["status"] == "pending"


def test_unlocated_standalone_not_materializable(tmp_path, monkeypatch):
    """spec §四.2：无引句且无坐标的疑点不可物化（如实拒绝）。"""
    monkeypatch.setenv("STORE_DB_PATH", str(tmp_path / "t.db"))
    s = ReviewStore(db_path=str(tmp_path / "t.db"))
    rid = s.create(filename="c.txt", category="procurement")
    s.update(rid, status="done", quality={"available": True,
                                          "observations": []},
             verify={"questions": [
                 _q("vq99", "待核实", "来源不明的疑问", quote="",
                    clause_id="")]},
             items=[])
    row = s.get(rid)
    key = afs.review_findings(row)["findings"][0]["finding_key"]
    out = afs.set_status(s, rid, finding_key=key, status="confirmed")
    assert out["status"] == "error"
    row = s.get(rid)
    assert not [i for i in row["items"]
                if i.get("tag_source") == "ai_finding"]
    # 争议（留痕）不受物化守卫影响
    out = afs.set_status(s, rid, finding_key=key, status="disputed")
    assert out["status"] == "ok"


def test_invalid_status_and_missing_finding(env_store):
    s, rid = env_store
    assert afs.set_status(s, rid, finding_key="nope",
                          status="confirmed")["status"] == "error"
    key = _merged_key(s, rid)
    assert afs.set_status(s, rid, finding_key=key,
                          status="bogus")["status"] == "error"


def test_finding_key_resolved_on_normalized_view(env_store):
    """键同源（读路径派生属性预堵）：GET 页面在 derive_claims_view 归一化
    视图上发键（claim_id 不写回 store），set_status 必须同源解析——
    否则 ver:{claim_id} 形态的键会 finding_not_found。"""
    from app.services.evidence import derive_claims_view
    s, rid = env_store
    row = s.get(rid)
    norm, _w, _cw = derive_claims_view(row)
    keys = [f["finding_key"] for f in afs.review_findings(norm)["findings"]]
    assert keys
    for k in keys:
        out = afs.set_status(s, rid, finding_key=k, status="viewed")
        assert out["status"] == "ok", f"页面键 {k} 解析失败"

# ---------------- 派生词族（牵连尽力检索） ----------------


def test_derive_keywords_from_quote():
    kws = derive_keywords_from_quote("违约金为合同总价款的30%，违约金上限未约定")
    assert kws and "违约金" in kws
    # 长词优先（更具体）
    assert len(kws) <= 4


def test_find_related_clauses_keywords_override():
    text = ("第一条 价款：合计100元。第二条 违约责任：违约金上限未约定。"
            "第三条 结算：逾期交付的，违约金按日加收。")
    p2 = text.index("第二条")
    p3 = text.index("第三条")
    clause_index = {"strategy": "numbered", "count": 3, "clauses": [
        {"id": "c01", "heading": "第一条", "start": 0, "end": p2,
         "chars": p2},
        {"id": "c02", "heading": "第二条", "start": p2, "end": p3,
         "chars": p3 - p2},
        {"id": "c03", "heading": "第三条", "start": p3, "end": len(text),
         "chars": len(text) - p3},
    ]}
    out = find_related_clauses(
        text, "ai_abcdef123456", clause_index=clause_index,
        exclude_clause_ids={"c02"}, keywords_override=["违约金", "逾期"])
    assert out, "派生词族应能在其他条款（c03）命中"
    assert all(e["excerpt"].find("违约金") >= 0 or e["excerpt"].find("逾期") >= 0
               for e in out)

# ---------------- 报告收录 ----------------


def _report_row():
    return {
        "id": "abc123def456",
        "filename": "采购.txt",
        "category": "procurement",
        "category_label": "采购合同",
        "status": "done",
        "created_at": "2026-11-05 10:00",
        "text": "全文不应出现在报告里哨兵句XYZ",
        "items": [{
            "id": "ai_xxx", "name": "【AI 发现】缺违约金上限",
            "status": "需关注", "note": "无上限约定",
            "quote": "违约金为合同总价款的30%",
            "tag_source": "ai_finding",
        }, {
            "id": "ai_yyy", "name": "【AI 发现】某事实存疑",
            "status": "已争议撤回", "note": "用户反悔",
            "quote": "q", "tag_source": "ai_finding",
        }],
        "scorecard": {"available": False, "reason": "no_llm_key"},
        "blind_candidates": [],
        "ai_finding_records": [
            {"title": "缺违约金上限", "status": "confirmed"},
        ],
    }


def test_report_includes_ai_finding_materials():
    doc = Document(io.BytesIO(build_report_docx(_report_row())))
    text = "\n".join(p.text for p in doc.paragraphs)
    # 物化条目进逐条明细（【AI 发现】前缀）
    assert "【AI 发现】缺违约金上限" in text
    # 争议撤回条目保留留痕
    assert "【AI 发现】某事实存疑" in text
    # 观察类确认记录进「人工决定与确认」节
    assert "人工决定与确认" in text
    assert "已人工拍板" in text
    assert "缺违约金上限" in text
    assert "已确认" in text
    # 范围文案（新默认）
    assert "AI 发现与确认记录" in text


def test_report_legacy_scope_note_bytes_unchanged():
    """旧记录自带范围说明按原样渲染（字节契约不破）。"""
    row = _report_row()
    row["export_scope_note"] = (
        "报告只含本次读到并展示的内容；未读部分不写入结论"
        "（含规则核查、参考评分与补盲；不含页面 AI 观察及追问）"
    )
    doc = Document(io.BytesIO(build_report_docx(row)))
    text = "\n".join(p.text for p in doc.paragraphs)
    assert "不含页面 AI 观察及追问" in text
