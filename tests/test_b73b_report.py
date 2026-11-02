# -*- coding: utf-8 -*-
"""B7-3b 报告草案章节测试（spec-b7-3-ui-report §五A/§六）。

覆盖面：
- 确定性契约（M4 红线）：同输入同字节；无修订活动 → 章节不渲染，
  与既有报告逐字节一致（老契约不破坏）
- 红线 5：重审提示每项必带；红线 6：未选定如实「未选定修改建议」
- 禁语过滤（§五A）：选定版命中 → 整体不入报告如实标注；委婉变体
  考卷回放（「建议签署」变体）必须拦截
- 牵连四态如实：尚未发起 / uncovered（no_match vs missing_source）/
  degraded / legacy；findings 禁语 → 隐藏+「结果不完整」明示；
  摘录（合同原文）原样不过滤
- 编排一致性（§四A）：查 A→切 B→导出→切回 A，报告跟随当时选定版
全程 mock，零 LLM 花费。
"""
from __future__ import annotations

import io
import json
from unittest import mock

import pytest

from docx import Document

from app.services import impact, revision
from app.services.clause_index import build_clause_index
from app.services.evidence import _clause_at
from app.services.report import (REVISION_REVIEW_NOTICE,
                                 build_report_docx)
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
    "declaration": "以下为文本起草辅助参考，不构成法律意见。",
}, ensure_ascii=False)

_FINDINGS_OK = json.dumps({"findings": [{"index": 0,
    "note": "该摘录与草案中定金罚则的约定存在不一致"}],
    "notice": "请注意核对赔偿口径"}, ensure_ascii=False)


def _item(item_id="unfair_terms") -> dict:
    return {"id": item_id, "name": "公平性", "status": "需关注",
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


def _view(rid="r-x") -> dict:
    """最小派生视图（章节外部分走既有节的缺省渲染）。"""
    return {"id": rid, "filename": "c.docx", "category": "procurement",
            "category_label": "采购合同", "status": "done",
            "created_at": "2026-11-02 10:00", "items": [], "scorecard": None}


def _doc_text(data: bytes) -> str:
    doc = Document(io.BytesIO(data))
    parts = [p.text for p in doc.paragraphs]
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                parts.append(cell.text)
    return "\n".join(parts)


# ---------- 确定性契约 + 老契约不破坏 ----------

def test_no_revision_activity_keeps_legacy_bytes(env_store):
    """无修订活动：章节不渲染；注入空列表与否字节逐一致（M4 契约不破坏）。"""
    store, rid = env_store
    section = impact.revision_report_section(store, rid)
    assert section == []
    assert build_report_docx(_view()) == build_report_docx(
        {**_view(), "revision_section": []})


def test_determinism_same_payload_same_bytes():
    """确定性契约（M4 红线）：同输入两次拼装 → 同字节。"""
    payload = [{
        "item_id": "unfair_terms", "item_name": "公平性",
        "item_status": "需关注", "quote": _C3_QUOTE,
        "draft": {"banned": False, "missing": False, "stance": "平衡",
                  "text": "折中方案文本。", "note": "双方各让一步",
                  "rationale": "三版差异说明。",
                  "assumptions": ["假设定金给付方为乙方"],
                  "declaration": "辅助参考，不构成法律意见。"},
        "impact": {"bound": {"draft_id": "d1", "stance": "平衡"},
                   "state": "ok", "legacy": False, "stale": False,
                   "findings": [{"index": 0, "note": "口径不一致"}],
                   "hidden_findings": 0, "notice": "",
                   "excerpts": [{"excerpt": _C3_QUOTE}], "covered": True},
    }]
    v1 = {**_view(), "revision_section": payload}
    assert build_report_docx(v1) == build_report_docx(dict(v1))


# ---------- 章节形态：选定 / 未选定 / 红线 ----------

def test_section_full_form_selected_with_impact(env_store):
    """有选定+牵连 ok：标题/免责/原文/修改文本/理由/假设/牵连对齐/
    重审提示逐项在位。"""
    store, rid = env_store
    draft_id = _select(store, rid)
    impact.analyze_impact(store, rid, "unfair_terms", draft_id,
                          llm_call=lambda *a: _FINDINGS_OK)
    section = impact.revision_report_section(store, rid)
    text = _doc_text(build_report_docx({**_view(), "revision_section": section}))
    assert "四、AI 修改建议（AI 起草辅助，不构成法律意见）" in text
    assert "【需关注】公平性" in text
    assert f"原文摘句：{_C3_QUOTE}。" in text
    assert "您选定的方案：平衡" in text
    assert "修改文本：折中方案文本。" in text
    assert "选择理由：三版差异说明。" in text
    assert "事实假设：假设定金给付方为乙方" in text
    assert "不构成法律意见" in text
    assert "牵连检查（AI 帮您看这处改动会不会牵动合同别处" in text
    # 真实分析的摘录来自词族命中的其他条款（第十二条），被改条款自身排除
    assert "摘录：第十二条 违约责任：违约方应赔偿守约方损失。" in text
    assert "该摘录与草案中定金罚则的约定存在不一致" in text
    # analyze 管线自带重审提示 notice（§五A 重审提示在牵连节也要在场）
    assert "选定草案≠解决问题" in text
    assert REVISION_REVIEW_NOTICE in text, "红线 5：重审提示必带"
    assert "五、人工决定与确认" not in text  # 无决定数据时该节照旧省略
    # 编号顺延：明细(三) → 草案(四)，后续节让位
    assert text.index("三、逐条明细") < text.index("四、AI 修改建议")


def test_section_no_selection_honest_line(env_store):
    """红线 6：生成过但未选定 → 如实「未选定修改建议」，不展开候选文本。"""
    store, rid = env_store
    _gen_ok(store, rid)
    section = impact.revision_report_section(store, rid)
    assert len(section) == 1 and section[0]["draft"] is None
    text = _doc_text(build_report_docx({**_view(), "revision_section": section}))
    assert "未选定修改建议" in text
    assert "折中方案文本" not in text, "未选定不得展开草案文本（不虚构）"
    assert REVISION_REVIEW_NOTICE in text
    assert "牵连检查：还没有检查过" in text


def test_section_assumptions_empty_labeled(env_store):
    """假设为空 → 「无补充假设」，不留空白。"""
    store, rid = env_store
    _select(store, rid)
    section = impact.revision_report_section(store, rid)
    section[0]["draft"]["assumptions"] = []
    text = _doc_text(build_report_docx({**_view(), "revision_section": section}))
    assert "事实假设：无补充假设" in text


def test_section_skips_items_without_quote(env_store):
    """无原文摘句的检查项不进章节（无对照锚）；有摘句的条目照常进。"""
    store, rid = env_store
    store.update(rid, status="done",
                 items=[_item(), {**_item("no_quote_item"), "quote": "",
                                  "name": "无锚项"}],
                 text=_TEXT, clause_index=_CLAUSE_INDEX)
    _select(store, rid, item_id="unfair_terms")
    section = impact.revision_report_section(store, rid)
    assert [e["item_id"] for e in section] == ["unfair_terms"]


# ---------- 禁语过滤（§五A + 委婉变体考卷回放） ----------

def test_banned_selected_version_excluded_whole(env_store):
    """选定版文本命中禁语 → 该版整体不入报告，如实标注；委婉变体
    （「建议签署」嵌入长句）同样拦截。
    直插库构造：生成层本就会把禁语草案整项降级（B7-0 先修禁令），
    正常路径选不出这种版——报告层过滤是纵深防御（老数据/落库后变体）。"""
    import sqlite3
    banned_batch = json.dumps({
        "versions": [
            {"stance": "稳妥", "text": "先协商修订。", "note": "最稳妥"},
            {"stance": "平衡", "text": "双方无异议，本合同建议签署推进。",
             "note": "平衡方案"},
            {"stance": "进取", "text": "主张罚则无效。", "note": "强势方案"},
        ],
        "rationale": "三版差异说明。",
        "assumptions": ["假设定金给付方为乙方"],
        "declaration": "辅助参考，不构成法律意见。",
    }, ensure_ascii=False)
    store, rid = env_store
    revision._connect().close()  # 触发 revision 两表建表
    with impact._connect() as conn:
        conn.execute(impact._SELECTED_DDL)  # selected_drafts 在 impact 写路径建
    con = sqlite3.connect(str(store._path))
    con.execute(
        "INSERT INTO revision_drafts (id, review_id, item_id, created_at,"
        " trace_id, original_quote, payload, status) VALUES (?,?,?,?,?,?,?,?)",
        ("d-banned", rid, "unfair_terms", 1.0, "t", _C3_QUOTE,
         banned_batch, "ok"))
    con.execute(
        "INSERT INTO selected_drafts (review_id, item_id, draft_id, stance,"
        " selected_at) VALUES (?,?,?,?,?)",
        (rid, "unfair_terms", "d-banned", "平衡", 2.0))
    con.commit()
    con.close()
    section = impact.revision_report_section(store, rid)
    d = section[0]["draft"]
    assert d["banned"] is True
    assert d["text"] == "" and d["rationale"] == ""
    text = _doc_text(build_report_docx({**_view(), "revision_section": section}))
    assert "建议签署" not in text, "禁语文本不得渗入报告"
    assert "未通过安全检查，内容未写入报告" in text
    assert REVISION_REVIEW_NOTICE in text
    section = impact.revision_report_section(store, rid)
    d = section[0]["draft"]
    assert d["banned"] is True
    assert d["text"] == "" and d["rationale"] == ""
    text = _doc_text(build_report_docx({**_view(), "revision_section": section}))
    assert "建议签署" not in text, "禁语文本不得渗入报告"
    assert "未通过安全检查，内容未写入报告" in text
    assert REVISION_REVIEW_NOTICE in text


def test_banned_finding_hidden_and_disclosed(env_store):
    """findings 说明命中禁语 → 该条隐藏+「结果不完整」明示；摘录原样。"""
    store, rid = env_store
    draft_id = _select(store, rid)
    banned = json.dumps({"findings": [
        {"index": 0, "note": f"摘录对照：{_C3_QUOTE}——该条款建议接受不必再改"},
    ], "excerpts": [{"excerpt": _C3_QUOTE}]}, ensure_ascii=False)
    impact.analyze_impact(store, rid, "unfair_terms", draft_id,
                          llm_call=lambda *a: banned)
    section = impact.revision_report_section(store, rid)
    imp = section[0]["impact"]
    assert imp["hidden_findings"] == 1 and imp["findings"] == []
    text = _doc_text(build_report_docx({**_view(), "revision_section": section}))
    assert "建议接受" not in text, "禁语说明不得渗入报告"
    assert "牵连检查结果不完整（1 条说明未通过安全检查）" in text
    assert "摘录：第十二条" in text, "合同原文是证据，不过滤（§五A）"
    assert "说明未通过安全检查，已隐藏" in text


# ---------- 牵连四态如实 ----------

def _insert_impact_row(store, rid, *, draft_id, stance, status, payload,
                       reason="", created):
    import sqlite3
    with impact._connect() as conn:
        conn.execute(impact._IMPACT_REPORTS_DDL)
    con = sqlite3.connect(str(store._path))
    con.execute(
        "INSERT INTO impact_reports (id, review_id, item_id, draft_id,"
        " created_at, trace_id, payload, status, reason, stance)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (f"ir-{created}", rid, "unfair_terms", draft_id, created, "t",
         payload, status, reason, stance))
    con.commit()
    con.close()


def test_impact_states_honest_in_report(env_store):
    """uncovered（两种）/ degraded / legacy 四态在报告中的如实文案。"""
    store, rid = env_store
    draft_id = _select(store, rid)

    # uncovered / no_match
    _insert_impact_row(store, rid, draft_id=draft_id, stance="平衡",
                       status="uncovered",
                       payload=json.dumps({"uncovered_code": "no_match"},
                                          ensure_ascii=False),
                       created=1.0)
    text = _doc_text(build_report_docx(
        {**_view(), "revision_section": impact.revision_report_section(store, rid)}))
    assert "不代表没有冲突" in text

    # uncovered / missing_source
    _insert_impact_row(store, rid, draft_id=draft_id, stance="平衡",
                       status="uncovered",
                       payload=json.dumps({"uncovered_reason": "缺少全文"},
                                          ensure_ascii=False),
                       created=2.0)
    text = _doc_text(build_report_docx(
        {**_view(), "revision_section": impact.revision_report_section(store, rid)}))
    assert "缺少可对照的完整内容" in text

    # degraded
    _insert_impact_row(store, rid, draft_id=draft_id, stance="平衡",
                       status="degraded", payload="{}", reason="timeout",
                       created=3.0)
    text = _doc_text(build_report_docx(
        {**_view(), "revision_section": impact.revision_report_section(store, rid)}))
    assert "这次分析没有完成，请在页面上重试一次" in text

    # legacy（无版别老记录，当前选定无绑定结果时不硬配）
    import sqlite3
    con = sqlite3.connect(str(store._path))
    con.execute("DELETE FROM selected_drafts WHERE review_id=?", (rid,))
    con.commit()
    con.close()
    _insert_impact_row(store, rid, draft_id=draft_id, stance="",
                       status="ok", payload=_FINDINGS_OK, created=4.0)
    section = impact.revision_report_section(store, rid)
    assert section[0]["impact"]["legacy"] is True
    text = _doc_text(build_report_docx({**_view(), "revision_section": section}))
    assert "这是一次较早的检查（当时没有记录针对哪个方案），建议重新检查一次" in text


# ---------- 编排一致性（§四A：报告跟随当时选定版） ----------

def test_report_follows_selection_sequence(env_store):
    """查 A→切 B→导出→切回 A：每次组装的数据与当时选定版一致，不串。"""
    store, rid = env_store
    r1 = _gen_ok(store, rid)
    r2 = _gen_ok(store, rid)
    impact.select_draft(store, rid, "unfair_terms", r1["draft_id"], "平衡")
    impact.analyze_impact(store, rid, "unfair_terms", r1["draft_id"],
                          llm_call=lambda *a: _FINDINGS_OK)
    impact.select_draft(store, rid, "unfair_terms", r2["draft_id"], "进取")
    impact.analyze_impact(store, rid, "unfair_terms", r2["draft_id"],
                          llm_call=lambda *a: _FINDINGS_OK)
    # 当前选定 B/进取：报告组装必须带 B 的牵连
    section = impact.revision_report_section(store, rid)
    assert section[0]["impact"]["bound"] == {
        "draft_id": r2["draft_id"], "stance": "进取"}
    assert section[0]["draft"]["stance"] == "进取"
    # 切回 A/平衡：组装随即跟随 A
    impact.select_draft(store, rid, "unfair_terms", r1["draft_id"], "平衡")
    section = impact.revision_report_section(store, rid)
    assert section[0]["impact"]["bound"] == {
        "draft_id": r1["draft_id"], "stance": "平衡"}
    assert section[0]["draft"]["stance"] == "平衡"


# ---------- 纯读红线 ----------

def test_section_is_pure_read_no_llm(env_store):
    def _explode(*a, **k):
        raise AssertionError("报告组装禁止触发 AI 调用")
    store, rid = env_store
    _select(store, rid)
    with mock.patch.object(revision, "_default_llm_call", _explode), \
         mock.patch.object(impact, "_default_llm_call", _explode):
        assert impact.revision_report_section(store, rid)
