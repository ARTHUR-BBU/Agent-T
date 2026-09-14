"""可信度 P2 batch2：F03 坏模型字段 / F06 摘句条款定位 / F07 上传体限位 /
F09 弱证据过强 note / 可选覆盖文案。
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.main import MAX_REQUEST_BODY_BYTES, BodySizeLimitMiddleware, app, _BodyTooLarge
from app.services import llm_ask, model_review, quality as quality_service, scorecard
from app.services.checklist import run_checklist
from app.services.clause_index import build_clause_index, locate_quote_clauses
from app.graph import pipeline

ROOT = Path(__file__).resolve().parents[1]


# ---------- F03：坏类型不得拖垮整次审查 / ask 500 ----------

def test_f03_segments_as_number_postprocess_degrades():
    items = [
        {"id": "payment", "name": "价款与支付", "segment": "C", "status": "通过", "note": "", "quote": ""},
    ]
    segs = scorecard.load_scorecard_config("procurement")["segments"]
    payload = {"scorecard": {"segments": 42, "summary": "ok"}, "_shape_ok": False}
    final = scorecard.postprocess(payload, items, segs)
    assert final["available"] is True
    assert final.get("incomplete") is True
    assert final.get("reason") == "incomplete_model_output"
    assert isinstance(final["segments"], list)


def test_f03_segments_as_object_and_score_infinity():
    items = [
        {"id": "payment", "name": "价款与支付", "segment": "C", "status": "通过", "note": "", "quote": ""},
    ]
    segs = scorecard.load_scorecard_config("procurement")["segments"]
    # Infinity via huge float from JSON-like parse
    payload = {
        "scorecard": {
            "summary": ["list", "summary"],
            "segments": [{"key": "C", "score": float("inf"), "comment": ["bad"]}],
        }
    }
    final = scorecard.postprocess(payload, items, segs)
    assert final["available"] is True
    c = next(s for s in final["segments"] if s["key"] == "C")
    assert c["score"] == 0
    assert final.get("incomplete") is True


def test_f03_scorecard_text_tolerates_bad_segments():
    assert model_review._scorecard_text({"scorecard": {"summary": "x", "segments": 42}}) == "x"
    assert model_review._scorecard_text({"scorecard": {"summary": ["a", "b"], "segments": []}})


def test_f03_ask_list_typed_fields_no_500():
    parsed = llm_ask._parse_structured(
        json.dumps(
            {
                "风险等级": "中",
                "这条在查啥": ["查", "付款"],
                "原文在哪": ["「签约即付」"],
                "问题是啥": ["有风险", "需改"],
                "建议怎么改": "改成分期",
                "还想问": ["质保呢"],
            },
            ensure_ascii=False,
        )
    )
    scrubbed = llm_ask._scrub_answer_dict(parsed)
    assert isinstance(scrubbed["问题是啥"], str)
    assert scrubbed["问题是啥"].strip()
    verified = llm_ask._verify_ask_answer(scrubbed, "签约即付全款。")
    assert isinstance(verified["原文在哪"], str)


def test_f03_model_review_bad_payload_keeps_going():
    items = [
        {
            "id": "payment",
            "name": "价款与支付",
            "segment": "C",
            "status": "需关注",
            "note": "n",
            "quote": "签约即付",
            "hits": ["签约即付"],
        }
    ]

    def chat(system: str, user: str) -> str:
        return json.dumps({"scorecard": {"segments": 42, "summary": "x"}}, ensure_ascii=False)

    out = model_review.run_model_review(
        text="签约即付全款。",
        items=items,
        category="procurement",
        chat_fn=chat,
    )
    sc = out["scorecard"]
    assert sc.get("available") is True or sc.get("reason") in {
        "incomplete_model_output",
        "parse_failed",
    }
    if sc.get("available"):
        assert sc.get("incomplete") is True or sc.get("degraded") is True


def test_f03_pipeline_model_review_exception_preserves_items():
    """坏输出若仍抛异常：node 软降级，不把整图打成 error。"""
    state = {
        "text": "甲方乙方约定：货款验收合格后支付。争议向法院起诉。",
        "items": [
            {"id": "payment", "name": "价款", "status": "通过", "segment": "C", "note": "", "quote": ""},
        ],
        "policies": [],
        "category": "procurement",
        "error": "",
    }

    def boom(**kwargs):
        raise TypeError("segments not iterable")

    with patch("app.graph.pipeline.run_model_review", side_effect=boom):
        out = pipeline.node_model_review(state)
    assert out["scorecard"]["available"] is False
    assert out["scorecard"]["reason"] == "incomplete_model_output"
    # node 不回写 items → checklist 结果仍在 state
    assert state["items"][0]["id"] == "payment"


# ---------- F06：摘句定位条款，不信任模型错号 ----------

def test_f06_wrong_clause_id_corrected_to_quote_location(monkeypatch):
    monkeypatch.setenv("QUALITY_ENABLED", "true")
    text = (
        "设备采购合同\n"
        "第一条 租金每月五千元。\n"
        "第二条 押金一万，合同期满退还。\n"
        "第三条 双方签字盖章生效。\n"
    )
    idx = build_clause_index(text)
    assert locate_quote_clauses(text, "租金每月五千元。", idx) == ["c01"]

    obs = [
        {
            "dimension": "completeness",
            "title": "租金条款",
            "quote": "租金每月五千元。",
            "clause_id": "c02",  # 错号但白名单存在
            "comment": "租金金额需与支付节奏对照确认。",
        }
    ]
    out = quality_service.run_quality(
        text=text,
        items=[],
        clause_index=idx,
        chat_fn=lambda s, u: json.dumps({"observations": obs}, ensure_ascii=False),
    )
    assert out["available"] is True
    assert len(out["observations"]) == 1
    assert out["observations"][0]["clause_id"] == "c01"
    assert out["observations"][0]["clause_ambiguous"] is False


def test_f06_duplicate_quote_marked_ambiguous(monkeypatch):
    monkeypatch.setenv("QUALITY_ENABLED", "true")
    text = (
        "合同\n"
        "第一条 租金每月五千元。补充说明。\n"
        "第二条 其他事项。\n"
        "第三条 重申：租金每月五千元。完。\n"
    )
    idx = build_clause_index(text)
    derived = locate_quote_clauses(text, "租金每月五千元。", idx)
    assert len(derived) >= 2

    obs = [
        {
            "dimension": "consistency",
            "title": "重复租金",
            "quote": "租金每月五千元。",
            "clause_id": "c02",
            "comment": "同一表述出现在多处，位置需人工确认。",
        }
    ]
    out = quality_service.run_quality(
        text=text,
        items=[],
        clause_index=idx,
        chat_fn=lambda s, u: json.dumps({"observations": obs}, ensure_ascii=False),
    )
    row = out["observations"][0]
    assert row["clause_id"] is None
    assert row["clause_ambiguous"] is True


# ---------- F07：请求体流式限位（multipart 解析前） ----------

def test_f07_content_length_oversize_413():
    client = TestClient(app)
    big = b"x" * (MAX_REQUEST_BODY_BYTES + 1)
    r = client.post(
        "/api/upload",
        files={"file": ("big.txt", big, "text/plain")},
        data={"category": "procurement"},
    )
    assert r.status_code == 413


def test_f07_streaming_receive_aborts_before_full_body():
    """无 Content-Length / 分块：超限后不再继续拉取后续 chunk。"""
    delivered = {"n": 0}
    chunk = b"y" * (512 * 1024)
    # 目标：超过 12MiB
    total_chunks = (MAX_REQUEST_BODY_BYTES // len(chunk)) + 3

    async def fake_receive():
        delivered["n"] += 1
        n = delivered["n"]
        if n <= total_chunks:
            return {
                "type": "http.request",
                "body": chunk,
                "more_body": n < total_chunks,
            }
        return {"type": "http.request", "body": b"", "more_body": False}

    sent: list[dict] = []

    async def fake_send(message):
        sent.append(message)

    async def sink_app(scope, receive, send):
        # 模拟 Starlette 拉满 body
        while True:
            msg = await receive()
            if msg["type"] != "http.request":
                break
            if not msg.get("more_body", False):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    mw = BodySizeLimitMiddleware(sink_app, max_bytes=MAX_REQUEST_BODY_BYTES)
    scope = {"type": "http", "method": "POST", "path": "/api/upload", "headers": []}

    async def run():
        await mw(scope, fake_receive, fake_send)

    asyncio.run(run())
    assert any(m.get("status") == 413 for m in sent if m["type"] == "http.response.start")
    # 关键：不得把全部 oversized chunks 都读完
    assert delivered["n"] < total_chunks
    assert delivered["n"] * len(chunk) > MAX_REQUEST_BODY_BYTES


def test_f07_business_file_limit_still_10mb():
    from app.api import routes

    assert routes.MAX_UPLOAD_BYTES == 10 * 1024 * 1024
    assert MAX_REQUEST_BODY_BYTES > routes.MAX_UPLOAD_BYTES


# ---------- F09：空白占位 / 付款 note 与事实对齐 ----------

def test_f09_blank_lease_term_not_pass():
    blank = "租赁期限：____。续租：____。"
    filled = "租赁期限：自2024年1月1日起至2026年12月31日止。续租：期满前30日书面提出可优先续租。"
    b = next(i for i in run_checklist(blank, "lease")["items"] if i["id"] == "lease_term")
    f = next(i for i in run_checklist(filled, "lease")["items"] if i["id"] == "lease_term")
    assert b["status"] == "需关注"
    assert f["status"] == "通过"


def test_f09_payment_notes_match_input_facts():
    after_accept = "甲方在验收合格后一次性支付全款，并另设质保金。"
    early = "签约即付全款，免质保。"
    good = next(i for i in run_checklist(after_accept, "procurement")["items"] if i["id"] == "payment")
    bad = next(i for i in run_checklist(early, "procurement")["items"] if i["id"] == "payment")
    assert good["status"] == "通过"
    assert "签约即付" not in (good.get("note") or "")
    assert "无质保" not in (good.get("note") or "")
    assert bad["status"] == "需关注"
    note = bad.get("note") or ""
    # note 须能被输入事实支撑：要么点名签约即付类，要么免质保类——不得宣称验收后支付
    assert ("签约" in note or "签订" in note or "免质保" in note or "质保" in note)
    assert "验收后再付" in note or "免质保" in note or "质保" in note


# ---------- 可选文案 ----------

def test_coverage_copy_shortened():
    js = (ROOT / "app/static/app.js").read_text(encoding="utf-8")
    assert "合同较长，本次只读到部分内容，结论供参考" in js
    assert "长合同超出分段阅读预算，观察为有限覆盖" not in js
    assert "模型摘句未通过原文核验，已隐藏" in js
