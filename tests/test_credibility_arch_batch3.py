"""可信度架构 batch3：EvidenceRef / 阶段落盘 / 事实材料 / 导出范围。

对应审计 A1、A5、A2、A3。Design B：模型不得改写规则档位。
界面文案用「证据引用/阅读范围/事实材料」，测试可引用代码名 EvidenceRef。
"""
from __future__ import annotations

import io
import json
from pathlib import Path
from unittest.mock import patch

from docx import Document
from fastapi.testclient import TestClient

from app.main import app
from app.graph import pipeline
from app.services import facts as facts_service
from app.services import quality as quality_service
from app.services.checklist import run_checklist
from app.services.clause_index import build_clause_index, map_items_to_clauses
from app.services.evidence import (
    attach_evidence_to_item,
    build_evidence,
    document_version_for,
    locate_quote_span,
    read_clause_label,
)
from app.services.report import build_report_docx
from app.services.store import store

ROOT = Path(__file__).resolve().parents[1]
client = TestClient(app)

_MINI = (
    "甲方：某某科技有限公司。乙方：某某贸易有限公司。\n"
    "合同总价人民币十万元整。签订日期：2024年3月1日。\n"
    "货款验收合格后支付。争议向法院起诉。适用中华人民共和国法律。"
)


# ---------- A1 EvidenceRef ----------

def test_evidence_ref_from_rule_hit_has_coords_and_version():
    text = "本房屋系转租所得，承租人已知悉。" + ("补充说明。" * 20)
    items = run_checklist(text, "lease")["items"]
    idx = build_clause_index(text)
    map_items_to_clauses(items, idx, text)
    doc_ver = document_version_for(text)
    lt = next(i for i in items if i["id"] == "lessor_title")
    attach_evidence_to_item(
        lt, text=text, parse_source="rules", document_version=doc_ver, clause_index=idx
    )
    ev = lt["evidence"]
    assert ev["document_version"] == doc_ver
    assert ev["parse_source"] == "rules"
    assert ev["verification"] in {"verified", "ambiguous"}
    assert isinstance(ev["start"], int) and isinstance(ev["end"], int)
    assert text[ev["start"] : ev["end"]]
    assert "转租" in (ev["quote"] or lt.get("quote") or "")


def test_evidence_locate_ambiguous_and_missing():
    text = "租金每月五千元。后文再写：租金每月五千元。"
    s, e, st = locate_quote_span(text, "租金每月五千元。")
    assert st == "ambiguous" and s is not None
    s2, e2, st2 = locate_quote_span(text, "天价违约金壹亿元")
    assert st2 == "missing" and s2 is None


def test_same_finding_traces_to_doc_version_and_coords():
    """同一发现可追到原文版本 + 坐标（放行条件）。"""
    text = "买方违约时定金不予退还。货款验收合格后分期支付。"
    doc_ver = document_version_for(text)
    ev = build_evidence(
        text=text,
        quote="定金不予退还",
        parse_source="rules",
        document_version=doc_ver,
    )
    assert ev["document_version"] == doc_ver
    assert text[ev["start"] : ev["end"]] == "定金不予退还"
    # 改字 → 版本变
    assert document_version_for(text + "。") != doc_ver


def test_design_b_model_never_overwrites_rule_status(monkeypatch):
    """Design B：model_review / quality 不得改写规则档位。"""
    state = {
        "text": _MINI,
        "items": [
            {
                "id": "payment",
                "name": "价款",
                "status": "通过",
                "segment": "C",
                "note": "",
                "quote": "验收合格后支付",
                "hits": ["验收合格后支付"],
            }
        ],
        "policies": [],
        "category": "procurement",
        "error": "",
        "clause_index": build_clause_index(_MINI),
        "document_version": document_version_for(_MINI),
    }
    snap = state["items"][0]["status"]

    def boom(**kwargs):
        # 即便模型想改 items，pipeline 也不回写
        kwargs.get("items")  # noqa: B018
        raise TypeError("bad model")

    with patch("app.graph.pipeline.run_model_review", side_effect=boom):
        out = pipeline.node_model_review(state)
    assert state["items"][0]["status"] == snap == "通过"
    assert "items" not in out or out.get("items") is None


# ---------- A5 stage persistence + parse reuse ----------

def test_pipeline_emits_rules_complete_partial_before_ai():
    stages: list[str] = []
    partials: list[str] = []

    def on_stage(s: str) -> None:
        stages.append(s)

    def on_partial(completion: str, fields: dict) -> None:
        partials.append(completion)
        if completion == "rules_complete":
            assert fields.get("items"), "规则完成必须带 items"
            assert all(
                i.get("status") in {"通过", "需关注", "未找到", "本类不适用"}
                for i in fields["items"]
            )

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
            _MINI.encode("utf-8"),
            category="procurement",
            on_stage=on_stage,
            on_partial=on_partial,
            parsed_text=_MINI,  # 复用解析
        )
    assert "rules_complete" in partials
    assert "ai_partial" in partials
    assert "fully_complete" in partials
    assert result["completion"] == "fully_complete"
    assert result["items"], "规则结果保留"
    # 证据挂载
    assert any(i.get("evidence") for i in result["items"])


def test_parse_reuse_skips_extract(monkeypatch):
    calls = {"n": 0}

    def boom(*a, **k):
        calls["n"] += 1
        raise AssertionError("should not re-extract")

    monkeypatch.setattr(pipeline, "extract_text", boom)
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
        out = pipeline.run_review(
            "t.txt", b"ignored", category="procurement", parsed_text=_MINI
        )
    assert calls["n"] == 0
    assert out["text"] == _MINI


def test_store_keeps_items_on_rules_complete_partial():
    """阶段落盘：rules_complete 写入后 items 可见且 status 仍 processing。"""
    rid = store.create(
        filename="t.txt",
        category="procurement",
        category_label="采购",
        status="processing",
        stage="scoring",
        items=[],
        scorecard={},
    )
    items = run_checklist(_MINI, "procurement")["items"]
    store.update(
        rid,
        items=items,
        completion="rules_complete",
        text=_MINI,
        text_preview=_MINI[:100],
    )
    row = store.get(rid)
    assert row["status"] == "processing"
    assert row["completion"] == "rules_complete"
    assert len(row["items"]) == len(items)
    d = client.get(f"/api/review/{rid}").json()
    assert d["completion"] == "rules_complete"
    assert d["items"]


# ---------- A2 fact materials ----------

def test_deterministic_facts_extract_parties_amounts_dates():
    facts = facts_service.extract_deterministic_facts(_MINI)
    kinds = {f["kind"] for f in facts}
    assert "party" in kinds or "amount" in kinds or "date" in kinds
    for f in facts:
        assert f.get("evidence", {}).get("verification") in {
            "verified",
            "ambiguous",
        }
        assert f["evidence"]["parse_source"] == "fact"


def test_consistency_receives_facts_when_map_observations_empty(monkeypatch):
    """A2：分段 map 空观察时，一致性轮素材仍含事实材料（金额对照）。"""
    monkeypatch.setenv("QUALITY_ENABLED", "true")
    # 两段都有不同金额；各段观察为空
    # 须超过 scorecard.MAX_CONTRACT_CHARS 才走分段 map
    from app.services.scorecard import MAX_CONTRACT_CHARS
    pad = "双方确认本条款内容并同意按约定履行。"
    para_a = "第一条 合同总价人民币十万元整。" + pad + "\n"
    para_b = "第二条 合同总价人民币二十万元整。" + pad + "\n"
    text = "长合同\n"
    while len(text) <= MAX_CONTRACT_CHARS + 500:
        text += para_a + para_b
    assert len(text) > MAX_CONTRACT_CHARS

    calls: list[str] = []

    def chat(system: str, user: str) -> str:
        calls.append(user)
        return json.dumps(
            {"observations": [], "facts": [], "pending_questions": []},
            ensure_ascii=False,
        )

    out = quality_service.run_quality(text=text, items=[], chat_fn=chat)
    assert out.get("facts"), "确定性事实至少应有一份（map 空观察也不丢材料）"
    assert any(f.get("kind") == "amount" for f in out["facts"])
    # 一致性轮素材应带上事实材料
    cons = [u for u in calls if "一致性轮" in u or "【一致性轮】" in u]
    assert cons, "长合同应触发一致性轮"
    assert any(
        ("十万元" in u or "二十万元" in u or "事实材料" in u or "事实/" in u)
        for u in cons
    ), cons[0][:600]


# ---------- A3 display/export scope ----------

def test_report_includes_export_scope_and_reading_scope():
    row = {
        "id": "abc123def456",
        "filename": "采购.txt",
        "category": "procurement",
        "category_label": "采购合同",
        "status": "done",
        "created_at": "2026-09-14 10:00",
        "text": "全文不应出现在报告里哨兵句XYZ",
        "items": [
            {
                "id": "payment",
                "name": "付款",
                "status": "需关注",
                "note": "预付",
                "quote": "签约即付",
            }
        ],
        "scorecard": {
            "available": True,
            "total": 70,
            "tier": {"label": "一般"},
            "summary": "参考",
            "segments": [],
            "disclaimer": "仅供参考",
            "coverage": {
                "limited": True,
                "chunks_total": 4,
                "chunks_reviewed": 4,
                "original_chars": 100000,
                "chars_covered": 24000,
                "unread_ranges": [[24000, 100000]],
                "truncation_reason": "max_segments_clip",
            },
        },
        "blind_candidates": [],
        "export_scope_note": (
            "报告只含本次读到并展示的内容；未读部分不写入结论"
            "（含规则核查、参考评分与补盲；不含页面 AI 观察及追问）"
        ),
    }
    data = build_report_docx(row)
    doc = Document(io.BytesIO(data))
    blob = "\n".join(p.text for p in doc.paragraphs)
    assert "报告只含本次读到并展示的内容" in blob
    assert "阅读范围" in blob and "只读到部分内容" in blob
    assert "全文不应出现在报告里哨兵句XYZ" not in blob
    assert "AI 观察" not in blob or "不含" in blob


def test_ui_copy_jiuge_terms_no_evidenceref_english():
    js = (ROOT / "app/static/app.js").read_text(encoding="utf-8")
    html = (ROOT / "app/static/index.html").read_text(encoding="utf-8")
    css = (ROOT / "app/static/styles.css").read_text(encoding="utf-8")
    surface = js + html
    assert "阅读范围" in surface
    assert "事实材料" in surface
    assert "看原文" in surface
    assert "报告只含本次读到并展示的内容" in surface
    assert "未抽出可核对事实" in surface
    # 界面字符串不得出现 EvidenceRef 英文学名（注释除外：抽检用户可见区）
    assert "EvidenceRef" not in html
    assert "#F5F5F7" in css or "F5F5F7" in css
    assert "FFE08A" in css or "#FFE08A" in css


def test_read_clause_label_helper():
    text = "第一条 甲\n内容一。\n第二条 乙\n内容二。\n第三条 丙\n内容三。\n"
    idx = build_clause_index(text)
    cov = {
        "limited": True,
        "chars_covered": 20,
        "original_chars": len(text),
        "unread_ranges": [[idx["clauses"][-1]["start"], len(text)]],
    }
    label = read_clause_label(idx, cov)
    assert "已读第" in label
