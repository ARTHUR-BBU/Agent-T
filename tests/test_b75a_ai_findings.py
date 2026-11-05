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
    """派生问题（source=quality_obs + source_ref=obs:{下标}）并入观察为
    一条发现；独立疑点单独成条。"""
    row = {
        "id": "r",
        "quality": {"available": True, "observations": [
            {"dimension": "completeness", "title": "缺验收标准",
             "quote": "验收按双方口头", "clause_id": "c01", "comment": "c"},
        ]},
        "verify": {"questions": [
            dict(_q("vq01", "缺验收标准", "请确认是否属实", source="quality_obs",
                    quote="验收按双方口头", clause_id="c01"),
                 source_ref="obs:0"),
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


def test_merge_pairs_by_obs_ref_not_title():
    """codex 三轮 P1-3：同标题不同条款的观察按下标配对，绝不按 title
    配（title 配对会串确认状态——交换顺序即复现）。"""
    row = {
        "id": "r",
        "quality": {"available": True, "observations": [
            {"dimension": "completeness", "title": "同名观察",
             "quote": "引句甲", "clause_id": "c01", "comment": ""},
            {"dimension": "completeness", "title": "同名观察",
             "quote": "引句乙", "clause_id": "c02", "comment": ""},
        ]},
        "verify": {"questions": [
            dict(_q("vq01", "同名观察", "请确认「同名观察」是否属实？",
                    source="quality_obs", quote="引句甲", clause_id="c01"),
                 source_ref="obs:0", status="confirmed"),
            dict(_q("vq02", "同名观察", "请确认「同名观察」是否属实？",
                    source="quality_obs", quote="引句乙", clause_id="c02"),
                 source_ref="obs:1", status="disputed"),
        ]},
        "items": [],
    }
    fs = afs.review_findings(row)["findings"]
    assert len(fs) == 2
    by_quote = {f["quote"]: f for f in fs}
    # 甲观察只继承甲问题的状态（下标配对，不串台）
    assert by_quote["引句甲"]["status"] == "confirmed"
    assert by_quote["引句甲"]["question_id"] == "vq01"
    assert by_quote["引句乙"]["status"] == "disputed"
    assert by_quote["引句乙"]["question_id"] == "vq02"


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
                 dict(_q("vq01", "缺违约金上限", "请确认是否属实",
                         source="quality_obs",
                         quote="违约金为合同总价款的30%", clause_id="c02"),
                      source_ref="obs:0")]},
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

# ---------------- codex 四轮整改回归（P1-1/P2-2/P2-3） ----------------


def test_rechecked_keeps_human_decision(env_store):
    """codex 四轮 P1-1：再核只改核对进度，不吞人的决定。
    ①决定记录优先 ②status 直陈 ③rechecked→状态表兜底。"""
    s, rid = env_store
    key = _merged_key(s, rid)
    # 统一入口确认（落状态表+决定记录），随后模拟再核把问题态打成 rechecked
    afs.set_status(s, rid, finding_key=key, status="confirmed")
    row = s.get(rid)
    for q in row["verify"]["questions"]:
        q["status"] = "rechecked"
        q["decision"] = {"choice": "confirm"}
    s.update(rid, verify=row["verify"])
    f = afs.review_findings(s.get(rid))["findings"][0]
    assert f["status"] == "confirmed", "再核后仍应显示已确认（可撤回）"
    assert f["check_state"] == "rechecked"

    # 决定优先于核对进度：决定=争议 时即便 rechecked 也显示已争议撤回
    row = s.get(rid)
    for q in row["verify"]["questions"]:
        q["decision"] = {"choice": "dispute"}
    s.update(rid, verify=row["verify"])
    f = afs.review_findings(s.get(rid))["findings"][0]
    assert f["status"] == "disputed" and f["check_state"] == "rechecked"

    # 无决定记录的 rechecked → 状态表兜底（此前确认落过表=confirmed）
    row = s.get(rid)
    for q in row["verify"]["questions"]:
        q["decision"] = None
    s.update(rid, verify=row["verify"])
    f = afs.review_findings(s.get(rid))["findings"][0]
    assert f["status"] == "confirmed"


def test_opinion_dispute_persists(env_store, tmp_path, monkeypatch):
    """codex 四轮 P2-3：观点类（无派生问题）的争议持久化——刷新不回退；
    恢复已阅同样落表。观点保持不可物化。"""
    monkeypatch.setenv("STORE_DB_PATH", str(tmp_path / "t2.db"))
    s = ReviewStore(db_path=str(tmp_path / "t2.db"))
    rid = s.create(filename="c.txt", category="procurement")
    s.update(rid, status="done",
             quality={"available": True, "observations": [
                 {"dimension": "impact", "title": "议价空间判断",
                  "quote": "", "clause_id": "", "comment": "观点正文"}]},
             verify={"questions": []}, items=[])
    row = s.get(rid)
    key = afs.review_findings(row)["findings"][0]["finding_key"]
    out = afs.set_status(s, rid, finding_key=key, status="disputed")
    assert out["status"] == "ok"
    f = afs.review_findings(s.get(rid))["findings"][0]
    assert f["status"] == "disputed", "观点争议必须持久化"
    assert not [i for i in s.get(rid)["items"]
                if i.get("tag_source") == "ai_finding"], "观点不可物化"
    # 恢复已阅
    afs.set_status(s, rid, finding_key=key, status="viewed")
    f = afs.review_findings(s.get(rid))["findings"][0]
    assert f["status"] == "viewed"


def test_materialize_carries_evidence(env_store):
    """codex 四轮 P2-2：确认物化时证据随行——item.evidence 保留原发现
    的证据对象（读路径证据链/主张编号按它派生），报告不再「无证据票据」。"""
    from app.services.evidence import build_evidence
    from app.services.evidence import document_version_for
    s, rid = env_store
    text = "违约金为合同总价款的30%"
    ev = build_evidence(text=text, quote=text, parse_source="quality",
                        document_version=document_version_for(text))
    ev["verification"] = "verified"
    row = s.get(rid)
    row["quality"]["observations"][0]["evidence"] = ev
    # 全文必须随卷（缺全文的证据经归一化定位不到会被降级清票——合理行为）
    s.update(rid, text=text, quality=row["quality"])
    key = _merged_key(s, rid)
    out = afs.set_status(s, rid, finding_key=key, status="confirmed")
    assert out["status"] == "ok"
    it = next(i for i in s.get(rid)["items"]
              if i.get("tag_source") == "ai_finding")
    assert isinstance(it.get("evidence"), dict)
    assert it["evidence"].get("evidence_id") == ev["evidence_id"]
    # 报告侧：有证据票据 → 「摘句已核验定位」，不再是「无证据票据」
    rrow = _report_row()
    rrow["items"] = [dict(it, name="【AI 发现】缺违约金上限")]
    doc = Document(io.BytesIO(build_report_docx(rrow)))
    text_out = "\n".join(p.text for p in doc.paragraphs)
    assert "摘句已核验定位" in text_out


# ---------------- codex 五轮整改回归（并发串行 + 观点权威统一） ----------------


def test_concurrent_confirms_keep_both_items(tmp_path, monkeypatch):
    """codex 五轮 P1：两条发现并发确认不得丢检查项——「读改写」全程持
    同一把 review 锁（与 confirm/recheck 串行化）。"""
    import threading

    monkeypatch.setenv("STORE_DB_PATH", str(tmp_path / "t3.db"))
    s = ReviewStore(db_path=str(tmp_path / "t3.db"))
    rid = s.create(filename="c.txt", category="procurement")
    s.update(rid, status="done",
             quality={"available": True, "observations": [
                 {"dimension": "completeness", "title": "发现甲",
                  "quote": "引句甲", "clause_id": "c01", "comment": ""},
                 {"dimension": "completeness", "title": "发现乙",
                  "quote": "引句乙", "clause_id": "c02", "comment": ""}]},
             verify={"questions": []}, items=[])
    keys = [f["finding_key"]
            for f in afs.review_findings(s.get(rid))["findings"]]
    assert len(keys) == 2
    barrier = threading.Barrier(2)
    results: list[dict] = []

    def _confirm(key: str) -> None:
        barrier.wait()  # 最大化并发窗口
        results.append(afs.set_status(s, rid, finding_key=key,
                                      status="confirmed"))

    t1 = threading.Thread(target=_confirm, args=(keys[0],))
    t2 = threading.Thread(target=_confirm, args=(keys[1],))
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    assert all(r["status"] == "ok" for r in results)
    ai_items = [i for i in s.get(rid)["items"]
                if i.get("tag_source") == "ai_finding"]
    assert len(ai_items) == 2, f"并发确认丢项：{len(ai_items)}"


def test_opinion_with_question_restore_viewed(tmp_path, monkeypatch):
    """codex 五轮 P2：带派生问题的观点——争议→恢复已阅必须生效
    （观点权威=状态表，不被问题行旧决定顶回）。"""
    monkeypatch.setenv("STORE_DB_PATH", str(tmp_path / "t4.db"))
    s = ReviewStore(db_path=str(tmp_path / "t4.db"))
    rid = s.create(filename="c.txt", category="procurement")
    s.update(rid, status="done",
             quality={"available": True, "observations": [
                 {"dimension": "impact", "title": "议价空间判断",
                  "quote": "押金2000元", "clause_id": "c04",
                  "comment": "观点正文"}]},
             verify={"questions": [
                 dict(_q("vq01", "议价空间判断", "请确认「议价空间判断」",
                         source="quality_obs", quote="押金2000元",
                         clause_id="c04"),
                      source_ref="obs:0", status="pending")]},
             items=[])
    row = s.get(rid)
    f0 = afs.review_findings(row)["findings"][0]
    assert f0["fact_type"] is False and f0["question_id"] == "vq01"
    key = f0["finding_key"]
    # 争议（观点：不产生决定记录，只落状态表）
    assert afs.set_status(s, rid, finding_key=key,
                          status="disputed")["status"] == "ok"
    assert afs.review_findings(s.get(rid))["findings"][0]["status"] == "disputed"
    # 恢复已阅——旧态不得顶回（四轮版实测复现点）
    assert afs.set_status(s, rid, finding_key=key,
                          status="viewed")["status"] == "ok"
    f2 = afs.review_findings(s.get(rid))["findings"][0]
    assert f2["status"] == "viewed", "恢复已阅必须持久化"
    # verify.questions 未被观点操作改写（不产生决定记录）
    q = s.get(rid)["verify"]["questions"][0]
    assert not isinstance(q.get("decision"), dict)


# ---------------- codex 三轮整改回归（P1-1/P1-2/P2-4/P2-5） ----------------


def _route_row():
    """路由级用例的假 done 记录（全局 store 单例 + TestClient）。"""
    from app.services.evidence import document_version_for
    text = "第四条 违约金为合同总价款的30%。"
    return {
        "filename": "c.txt", "category": "procurement", "status": "done",
        "stage": "done", "created_at": "2026-11-05 10:00",
        "scorecard": {}, "blind_candidates": [], "blind_skipped_messages": [],
        "blind_skipped_reason": None, "blind_enabled": False,
        "text": text, "document_version": document_version_for(text),
        "policies": [], "error": None,
        "items": [],
        "quality": {"available": True, "observations": [
            {"dimension": "completeness", "title": "缺违约金上限",
             "quote": "违约金为合同总价款的30%", "clause_id": "c04",
             "comment": "无上限"}]},
        "verify": {"available": True, "reason": None, "questions": [
            {"id": "vq01", "source": "quality_obs", "source_ref": "obs:0",
             "source_subject_key": "k",
             "question": "请确认：「缺违约金上限」是否属实？",
             "title": "缺违约金上限", "clause_id": "c04",
             "quote": "违约金为合同总价款的30%", "verification": "verified",
             "status": "pending"}]},
        "objections": {"available": True, "objections": []},
    }


def test_route_confirm_then_review_get_200():
    """codex 三轮 P1-1：确认→物化→GET /api/review 必须仍 200。

    旧响应 schema 只认 tag_source rule/blind 与四档 status，物化项
    （ai_finding/已争议撤回）会把读接口打成 500。"""
    from app.services.store import store as global_store
    from tests.test_evidence_claims import client  # noqa: F401 复用 TestClient
    rid = global_store.create(**_route_row())
    row = global_store.get(rid)
    key = afs.review_findings(row)["findings"][0]["finding_key"]
    resp = client.post(f"/api/review/{rid}/ai-finding/status",
                       json={"finding_key": key, "status": "confirmed"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True
    got = client.get(f"/api/review/{rid}")
    assert got.status_code == 200, got.text
    body = got.json()
    ai_items = [i for i in body["items"] if i.get("tag_source") == "ai_finding"]
    assert len(ai_items) == 1 and ai_items[0]["status"] == "需关注"
    assert body["ai_findings"]["available"] is True
    # 撤回后 GET 同样 200（「已争议撤回」档位可序列化）
    disp = client.post(f"/api/review/{rid}/ai-finding/status",
                       json={"finding_key": key, "status": "disputed"})
    assert disp.status_code == 200 and disp.json()["ok"] is True
    got2 = client.get(f"/api/review/{rid}")
    assert got2.status_code == 200
    assert any(i["status"] == "已争议撤回" for i in got2.json()["items"])


def test_withdrawn_item_select_and_impact_rejected(env_store):
    """codex 三轮 P1-2：撤回后选定/牵连入口关死（item_disabled），
    恢复确认后资格复原。生成/追问入口已有同判据闸。"""
    from app.services.impact import analyze_impact, select_draft
    s, rid = env_store
    key = _merged_key(s, rid)
    out = afs.set_status(s, rid, finding_key=key, status="confirmed")
    ai_id = out["item_id"]
    afs.set_status(s, rid, finding_key=key, status="disputed")
    r1 = select_draft(s, rid, ai_id, "draft_x", "平衡")
    assert r1["status"] == "not_supported" and r1["reason"] == "item_disabled"
    r2 = analyze_impact(s, rid, ai_id, "draft_x", llm_call=lambda *a: "")
    assert r2["status"] == "not_supported" and r2["reason"] == "item_disabled"
    # 恢复确认 → 资格复原（选定走到下一层校验=草案不存在）
    afs.set_status(s, rid, finding_key=key, status="confirmed")
    r3 = select_draft(s, rid, ai_id, "draft_x", "平衡")
    assert r3["status"] == "not_found" and r3["reason"] == "draft_not_found"


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


def test_report_withdrawn_not_counted_as_risk():
    """codex 三轮 P2-4：已争议撤回退出风险统计——不进「需关注与未找到」
    表、不算「无法识别档位从严处理」，结论行单列留痕计数。"""
    doc = Document(io.BytesIO(build_report_docx(_report_row())))
    text = "\n".join(p.text for p in doc.paragraphs)
    assert "无法识别档位" not in text
    assert "按需关注处理" not in text
    assert "已争议撤回 1 项" in text
    # 二节汇总表只收真正风险项（撤回条目不进表）
    table_text = "\n".join(
        c.text for t in doc.tables for r in t.rows for c in r.cells)
    assert "【AI 发现】缺违约金上限" in table_text
    assert "【AI 发现】某事实存疑" not in table_text


def test_report_ai_opinions_section():
    """codex 三轮 P2-5：观点类发现导出「AI 观点」小节（无观点不渲染）。"""
    row = _report_row()
    row["ai_finding_opinions"] = [
        {"title": "押金退还条件空悬", "comment": "观点唯一标记OPX9Q",
         "dimension_label": "对您的影响"},
    ]
    doc = Document(io.BytesIO(build_report_docx(row)))
    text = "\n".join(p.text for p in doc.paragraphs)
    assert "AI 观点（仅供参考）" in text
    assert "观点唯一标记OPX9Q" in text
    # 无观点 → 整节不渲染（字节契约）
    row2 = _report_row()
    doc2 = Document(io.BytesIO(build_report_docx(row2)))
    text2 = "\n".join(p.text for p in doc2.paragraphs)
    assert "AI 观点（仅供参考）" not in text2


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
