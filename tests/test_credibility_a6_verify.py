"""可信度 A6：有界主动核验（疑点→取证→核对→问你→再审）。

Design B：主动核查不得改写规则清单四档。
预算/次数硬封顶。UI 文案规格在前端（需你确认 / 待核实），本文件钉后端闭环。
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import verify as verify_service
from app.services.clause_index import build_clause_index
from app.services.store import store
from tests.helpers import wait_review_done

client = TestClient(app)

_TEXT = (
    "甲方：某某科技有限公司。乙方：某某贸易有限公司。\n"
    "第一条 付款。货款验收合格后支付。\n"
    "第二条 异议。乙方应于收货后七日内提出书面异议。\n"
    "合同总价人民币十万元整。签订日期：2024年3月1日。\n"
)


def _seed_review(**extra):
    idx = build_clause_index(_TEXT)
    items = [
        {
            "id": "payment",
            "name": "价款支付",
            "status": "需关注",
            "note": "付款条件需关注",
            "quote": "货款验收合格后支付",
            "hits": ["验收合格后支付"],
            "primary_clause_id": "c01",
            "clause_ids": ["c01"],
        },
        {
            "id": "party",
            "name": "主体",
            "status": "通过",
            "note": "",
            "quote": "甲方：某某科技有限公司",
            "hits": [],
            "primary_clause_id": "c01",
            "clause_ids": ["c01"],
        },
    ]
    quality = {
        "available": True,
        "observations": [
            {
                "dimension": "impact",
                "title": "异议期偏短",
                "quote": "乙方应于收货后七日内提出书面异议。",
                "clause_id": "c02",
                "comment": "七日偏短，建议延长。",
                "needs_confirm": True,
            }
        ],
        "pending_questions": ["验收标准是否另附清单？"],
        "facts": [],
        "disclaimer": "以上为 AI 观察，仅供参考。",
    }
    verify = verify_service.run_bounded_verify(
        text=_TEXT,
        items=items,
        quality=quality,
        blind_candidates=[],
        facts=[],
        clause_index=idx,
        document_version="demo:ver",
    )
    rid = store.create(
        filename="t.txt",
        category="procurement",
        category_label="采购",
        status="done",
        stage="done",
        items=items,
        scorecard={"available": False},
        blind_candidates=[],
        blind_enabled=False,
        quality=quality,
        text=_TEXT,
        clause_index=idx,
        document_version="demo:ver",
        facts=[],
        verify=verify,
        completion="fully_complete",
        **extra,
    )
    return rid, items, verify


# ---------- unit: loop shape ----------

def test_clause_by_id_fetch_and_verify_quote():
    idx = build_clause_index(_TEXT)
    # preamble 占 c01；异议条为 c03
    clause = verify_service.clause_by_id(_TEXT, idx, "c03")
    assert clause is not None
    assert "七日内提出书面异议" in clause["text"]
    ev = verify_service.verify_quote_against_text(
        _TEXT,
        "乙方应于收货后七日内提出书面异议。",
        clause_id="c03",
        clause_index=idx,
        document_version="v1",
    )
    assert ev["verification"] in {"verified", "ambiguous"}
    assert ev["document_version"] == "v1"


def test_run_bounded_verify_emits_pending_confirm_questions():
    idx = build_clause_index(_TEXT)
    out = verify_service.run_bounded_verify(
        text=_TEXT,
        items=[{"id": "payment", "name": "价款", "status": "需关注", "quote": "货款验收合格后支付", "primary_clause_id": "c01"}],
        quality={
            "observations": [
                {
                    "dimension": "impact",
                    "title": "异议期偏短",
                    "quote": "乙方应于收货后七日内提出书面异议。",
                    "clause_id": "c02",
                    "comment": "七日偏短",
                    "needs_confirm": True,
                }
            ],
            "pending_questions": ["验收标准是否另附？"],
        },
        clause_index=idx,
    )
    assert out["available"] is True
    assert out["rounds_used"] == 1
    assert out["disclaimer"] == "主动核查只提疑点，不改变清单规则档"
    assert out["questions"]
    assert all(q["status"] == "pending" for q in out["questions"])
    # 至少覆盖 quality_obs / pending
    sources = {q["source"] for q in out["questions"]}
    assert "quality_obs" in sources
    assert "pending" in sources
    # 有摘句的应能核对
    with_quote = [q for q in out["questions"] if q.get("quote")]
    assert with_quote
    assert any(q["verification"] in {"verified", "ambiguous", "missing"} for q in with_quote)


def test_budget_caps_rounds_and_questions(monkeypatch):
    monkeypatch.setenv("VERIFY_MAX_ROUNDS", "2")
    monkeypatch.setenv("VERIFY_MAX_QUESTIONS", "2")
    monkeypatch.setenv("VERIFY_MAX_CLAUSE_FETCHES", "1")
    idx = build_clause_index(_TEXT)
    quality = {
        "observations": [
            {
                "dimension": "impact",
                "title": f"观察{i}",
                "quote": "乙方应于收货后七日内提出书面异议。",
                "clause_id": "c02",
                "comment": "x",
                "needs_confirm": True,
            }
            for i in range(5)
        ],
        "pending_questions": ["Q1", "Q2", "Q3"],
    }
    out1 = verify_service.run_bounded_verify(
        text=_TEXT, items=[], quality=quality, clause_index=idx
    )
    assert len(out1["questions"]) <= 2
    assert out1["clause_fetches_used"] <= 1
    out2 = verify_service.run_bounded_verify(
        text=_TEXT, items=[], quality=quality, clause_index=idx, prior=out1
    )
    assert out2["rounds_used"] == 2
    out3 = verify_service.run_bounded_verify(
        text=_TEXT, items=[], quality=quality, clause_index=idx, prior=out2
    )
    assert out3["reason"] == "budget_exceeded"
    assert out3["rounds_used"] == 2


def test_confirm_then_recheck_path_and_design_b():
    rid, items, verify = _seed_review()
    snap = [(i["id"], i["status"]) for i in items]
    qid = verify["questions"][0]["id"]

    # confirm
    r = client.post(
        f"/api/review/{rid}/confirm",
        json={"question_id": qid, "choice": "confirm", "human_note": ""},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["verify"]["questions"][0]["status"] == "confirmed"

    row = store.get(rid)
    assert [(i["id"], i["status"]) for i in row["items"]] == snap

    # recheck
    r2 = client.post(
        f"/api/review/{rid}/reverify",
        json={"question_id": qid},
    )
    assert r2.status_code == 200, r2.text
    body2 = r2.json()
    assert body2["ok"] is True
    assert body2["rule_statuses_unchanged"] is True
    assert body2["verify"]["questions"][0]["status"] == "rechecked"
    assert body2["verify"]["questions"][0]["recheck_count"] == 1

    row2 = store.get(rid)
    assert [(i["id"], i["status"]) for i in row2["items"]] == snap
    # 禁止改判：通过项仍通过
    assert next(i for i in row2["items"] if i["id"] == "party")["status"] == "通过"
    assert next(i for i in row2["items"] if i["id"] == "payment")["status"] == "需关注"


def test_recheck_budget_per_question(monkeypatch):
    monkeypatch.setenv("VERIFY_MAX_RECHECK_PER_Q", "1")
    monkeypatch.setenv("VERIFY_MAX_ROUNDS", "10")
    rid, items, verify = _seed_review()
    qid = verify["questions"][0]["id"]
    assert client.post(
        f"/api/review/{rid}/confirm",
        json={"question_id": qid, "choice": "dispute", "human_note": "摘句需再看"},
    ).json()["ok"]
    assert client.post(
        f"/api/review/{rid}/reverify", json={"question_id": qid}
    ).json()["ok"]
    r = client.post(f"/api/review/{rid}/reverify", json={"question_id": qid})
    assert r.status_code == 200
    assert r.json()["ok"] is False
    assert "次数" in (r.json().get("error") or "")


def test_get_verify_lists_pending_and_budget():
    rid, _, verify = _seed_review()
    r = client.get(f"/api/review/{rid}/verify")
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is True
    assert body["budget"]["questions_pending"] >= 1
    assert body["disclaimer"] == "主动核查只提疑点，不改变清单规则档"
    # review summary 也带 verify
    summ = client.get(f"/api/review/{rid}").json()
    assert summ["verify"] is not None
    assert summ["verify"]["questions"]


def test_design_b_pipeline_verify_never_overwrites_rule_status():
    """pipeline 跑完 verify 后规则档位与 checklist 产出一致。"""
    from app.graph import pipeline
    from app.services.checklist import run_checklist

    checklist = run_checklist(_TEXT, "procurement")
    snap = {i["id"]: i["status"] for i in checklist["items"]}

    with patch(
        "app.graph.pipeline.run_model_review",
        return_value={
            "scorecard": {"available": False, "reason": "no_llm_key"},
            "blind_candidates": [],
            "blind_skipped_messages": [],
            "blind_skipped_reason": None,
            "blind_enabled": False,
        },
    ):
        result = pipeline.run_review(
            "t.txt",
            _TEXT.encode("utf-8"),
            category="procurement",
            parsed_text=_TEXT,
        )
    assert result.get("verify"), "A6 verify 应随 fully_complete 产出"
    for it in result["items"]:
        assert it["status"] == snap[it["id"]]
    # 文案铁律：不出现改判暗示
    blob = json.dumps(result.get("verify"), ensure_ascii=False)
    assert "改判" not in blob
    assert "盖章" not in blob


def test_ui_copy_locked_strings_present():
    """前端锁定文案：需你确认 / 待核实 / 流程 / 按钮 / 旁注 / 空态。"""
    root = Path(__file__).resolve().parents[1]
    js = (root / "app/static/app.js").read_text(encoding="utf-8")
    css = (root / "app/static/styles.css").read_text(encoding="utf-8")
    html = (root / "app/static/index.html").read_text(encoding="utf-8")
    assert "需你确认" in js
    assert 'textContent = "待核实"' in js
    assert "疑点" in js and "取证" in js and "核对" in js and "问你" in js
    assert "补充说明" in js and "确认无误" in js and "再审本条" in js
    assert "主动核查只提疑点，不改变清单规则档" in js
    assert "暂无需要确认的事项" in js
    assert "系统已改判" not in js
    assert "#F5F5F7" in css
    assert "verify-badge" in css
    assert "aria-label=\"需你确认\"" in html
