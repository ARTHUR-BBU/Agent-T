"""A6 门禁整改回归（外部审核三轮后 grok 团队 verify.py 的双门禁打回项）：

- P1-1 法务五条⑤金额跨条款：已核实的金额事实必须进分流（旧实现直接
  continue 剔除，矛盾恰发生在两个各自真实的事实之间；旧测试直调
  classify_triage 绕过采集层掩盖——本文件全部走 run_bounded_verify 链路）
- P2-2 证据票据归属：摘句不在声称条款内而由全文兜底命中时，票据按真实
  坐标派生归属，不挂声称的条款
- P2-3 trigger_verify 锁内重取 row：并发 confirm 不被 stale prior 冲掉
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import verify as verify_service
from app.services.clause_index import build_clause_index
from app.services.evidence import build_evidence
from app.services.store import store

client = TestClient(app)

# 两条款合同：总价在 c01、结算价在 c02——各自真实、互相矛盾
_TEXT_MISMATCH = (
    "甲方：某公司。乙方：某供应商。\n"
    "第一条 合同总价：人民币十万元整。\n"
    "第二条 结算条款：最终按结算金额人民币八万元整支付。\n"
    "本合同一式两份。\n"
)

_FACTS_MISMATCH = [
    {
        "kind": "amount",
        "label": "金额",
        "value": "人民币十万元整",
        "evidence": {
            "quote": "人民币十万元整",
            "verification": "verified",
            "clause_id": "c01",
            "parse_source": "fact",
        },
    },
    {
        "kind": "amount",
        "label": "金额",
        "value": "人民币八万元整",
        "evidence": {
            "quote": "人民币八万元整",
            "verification": "verified",
            "clause_id": "c02",
            "parse_source": "fact",
        },
    },
]


def test_full_chain_amount_cross_mismatch_asks_human():
    """P1-1 链路级：两条各自 verified+带条款的金额事实互相矛盾时，
    run_bounded_verify 必须产出 amount_cross must_human（旧实现在采集层
    把 verified 事实 continue 掉，五条⑤死路）。"""
    idx = build_clause_index(_TEXT_MISMATCH)
    out = verify_service.run_bounded_verify(
        text=_TEXT_MISMATCH,
        items=[],
        quality={},
        blind_candidates=[],
        facts=_FACTS_MISMATCH,
        clause_index=idx,
        document_version="gate:ver",
    )
    assert out["available"] is True
    rules = [e.get("triage_rule") for e in out.get("triage_log") or []]
    assert "amount_cross" in rules, f"五条⑤必须进分流记账：{rules}"
    entry = next(e for e in out["triage_log"] if e.get("triage_rule") == "amount_cross")
    assert entry["triage"] == "must_human", f"金额矛盾必须人审：{entry}"
    # 待办里也要有对应问题（该问的要问）
    refs = [q.get("source_ref") or "" for q in out.get("questions") or []]
    assert any("fact:" in r for r in refs), f"金额矛盾须进待确认：{refs}"


def test_full_chain_amount_consistent_stays_silent():
    """对照：金额跨条款一致且各自 verified → machine_silent（不打扰人）。"""
    text = _TEXT_MISMATCH.replace("人民币八万元整", "人民币十万元整")
    facts = [
        {
            "kind": "amount",
            "label": "金额",
            "value": "人民币十万元整",
            "evidence": {
                "quote": "人民币十万元整",
                "verification": "verified",
                "clause_id": "c01",
                "parse_source": "fact",
            },
        },
        {
            "kind": "amount",
            "label": "金额",
            "value": "人民币十万元整",
            "evidence": {
                "quote": "人民币十万元整",
                "verification": "verified",
                "clause_id": "c02",
                "parse_source": "fact",
            },
        },
    ]
    idx = build_clause_index(text)
    out = verify_service.run_bounded_verify(
        text=text, items=[], quality={}, blind_candidates=[],
        facts=facts, clause_index=idx, document_version="gate:ver",
    )
    rules = [e.get("triage_rule") for e in out.get("triage_log") or []]
    assert "amount_cross" in rules
    entry = next(e for e in out["triage_log"] if e.get("triage_rule") == "amount_cross")
    assert entry["triage"] == "machine_silent", f"一致金额不得打扰人：{entry}"


def test_evidence_ticket_binds_real_clause_not_claimed():
    """P2-2：摘句实际在 c02 而 suspect 声称 c01（取证成功）时，全文兜底
    命中的证据必须按真实坐标派生归属，不得挂声称的 c01。"""
    text = (
        "甲方：某公司。乙方：某供应商。\n"
        "第一条 合同总价：人民币十万元整。\n"
        "第二条 违约责任：乙方逾期交付的，应支付合同总额百分之二十的违约金。\n"
        "本合同一式两份。\n"
    )
    idx = build_clause_index(text)
    quote = "应支付合同总额百分之二十的违约金"  # 实际在 c02
    suspect = {
        "source": "quality_obs",
        "source_ref": "obs:0",
        "question": "请确认违约金条款",
        "title": "违约金",  # 价值判断词：不按价款机器放行（P2-1 同闸）
        "clause_id": "c01",  # 声称错号
        "quote": quote,
        "parse_source": "quality",
    }
    out = verify_service.run_bounded_verify(
        text=text,
        items=[],
        quality={"available": True, "observations": [{
            "dimension": "impact", "title": "违约金", "quote": quote,
            "clause_id": "c01", "comment": "比例偏高。", "needs_confirm": True,
        }]},
        blind_candidates=[],
        facts=[],
        clause_index=idx,
        document_version="gate:ver",
    )
    qs = out.get("questions") or []
    assert qs, "违约金价值判断必须进人审待办（P2-1）"
    ev = qs[0].get("evidence") or {}
    assert ev.get("clause_id") == "c02", (
        f"证据票据必须按真实坐标归属 c02，实际 {ev.get('clause_id')}"
    )


def test_trigger_verify_rereads_row_inside_lock():
    """P2-3：trigger_verify 在锁内重取 row——并发的 confirm 写入不得被
    锁外 stale prior 冲回 pending。"""
    from tests.helpers import wait_review_done  # noqa: F401 复用客户端上下文

    # 直接种一版带 confirmed 问题的 verify 状态：模拟 confirm 已发生
    q_confirmed = {
        "id": "vq01", "source": "quality_obs", "source_ref": "obs:0",
        "question": "请确认", "title": "t", "clause_id": None, "quote": "",
        "evidence": {}, "verification": "unverified", "fact_ok": None,
        "status": "confirmed", "triage": "must_human",
        "triage_reason": "r", "triage_rule": "default",
    }
    rid = store.create(
        filename="a.txt", category="lease", status="done", stage="done",
        created_at="2026-09-15 10:00",
        text="第一条 测试。\n第二条 结束。\n",
        items=[], scorecard={}, blind_candidates=[], blind_skipped_messages=[],
        blind_skipped_reason=None, blind_enabled=False, quality={}, policies=[],
        error=None, precheck=None,
        verify={
            "available": True, "reason": None, "rounds_used": 1,
            "max_rounds": 3, "clause_fetches_used": 0, "max_clause_fetches": 24,
            "max_questions": 8, "max_recheck_per_question": 2,
            "questions": [q_confirmed], "triage_log": [], "document_version": "v",
        },
    )
    # 包装 store.get：锁内第二次调用返回 confirm 已写入的行（模拟并发窗口：
    # trigger 在锁外拿到旧 row 后、拿锁前，confirm 把问题标记为 confirmed）
    original_get = store.get
    calls = {"n": 0}

    def racing_get(rid_: str):
        row = original_get(rid_)
        calls["n"] += 1
        if calls["n"] >= 2:  # 第一次=_require_done_row（锁外）；之后=锁内重取
            row = dict(row)
            row["verify"] = {
                **(row.get("verify") or {}),
                "questions": [{**q_confirmed, "status": "confirmed"}],
            }
        return row

    store.get = racing_get  # type: ignore[method-assign]
    try:
        r = client.post(f"/api/review/{rid}/verify")
        assert r.status_code == 200, r.text
    finally:
        store.get = original_get  # type: ignore[method-assign]
    body = r.json()
    statuses = [q["status"] for q in body.get("questions") or [] if q.get("id") == "vq01"]
    assert statuses == ["confirmed"], (
        f"人工确认不得被 stale prior 冲掉：{statuses}"
    )
