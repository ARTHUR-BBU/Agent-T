"""可信度 A6 分流补丁：法务五条人审边界 + 九哥词表（补盲≠待核实）。

词表钉死：
| 区         | 标签              | 旁注                         |
| 规则清单   | 需关注（琥珀）    | —                            |
| 补盲       | 补盲（淡紫）      | 候选，需人工确认             |
| 需你确认   | 待核实（灰描边）  | 主动核查只提疑点，不改变清单规则档 |

禁止补盲区出现「待核实」。
"""
from __future__ import annotations

from pathlib import Path

from app.services import verify as verify_service
from app.services.clause_index import build_clause_index
from app.services.evidence import build_evidence

_TEXT = (
    "甲方：某某科技有限公司。乙方：某某贸易有限公司。\n"
    "第一条 付款。货款验收合格后支付。合同总价人民币十万元整。\n"
    "第二条 异议。乙方应于收货后七日内提出书面异议。\n"
    "签订日期：2024年3月1日。\n"
)

_TEXT_AMOUNT_MISMATCH = (
    "甲方：甲。乙方：乙。\n"
    "第一条 总价人民币十万元整。\n"
    "第五条 结算金额人民币八万元整。\n"
)


def _idx(text: str = _TEXT):
    return build_clause_index(text)


# ---------- 九哥词表：补盲 ≠ 待核实 ----------

def test_ui_word_table_blind_not_pending_verify_label():
    """补盲徽章必须是「补盲」+旁注「候选，需人工确认」；禁止补盲区写「待核实」。"""
    root = Path(__file__).resolve().parents[1]
    js = (root / "app/static/app.js").read_text(encoding="utf-8")
    css = (root / "app/static/styles.css").read_text(encoding="utf-8")

    # 补盲列表徽章
    assert 'blindBadge.textContent = "补盲"' in js
    assert "候选，需人工确认" in js
    # meta 统计行用「补盲」
    assert "· 补盲 ${nBlind} 项" in js or "· 补盲 " in js

    # 「待核实」只允许出现在需你确认（verify）渲染路径
    lines = js.splitlines()
    blind_zone_hits = []
    for i, line in enumerate(lines, 1):
        if "待核实" not in line:
            continue
        # 允许：verify 面板预算行 / verify-badge / 注释说明分隔
        stripped = line.strip()
        if "verify" in stripped or "VERIFY" in stripped or "需你确认" in stripped:
            continue
        if stripped.startswith("//") and ("九哥" in stripped or "词表" in stripped or "留给" in stripped):
            continue
        if 'badge.textContent = "待核实"' in stripped:
            # 必须位于 renderVerify / verify-badge 上下文附近
            window = "\n".join(lines[max(0, i - 30) : i])
            assert "verify-badge" in window or "renderVerify" in window or "verify-item" in window, (
                f"line {i} 待核实不在需你确认区: {stripped}"
            )
            continue
        if "待核实 " in stripped and "questions_pending" in stripped:
            continue
        blind_zone_hits.append((i, stripped[:160]))

    assert not blind_zone_hits, f"补盲区禁出现待核实: {blind_zone_hits}"

    # 反例：列表/详情不得再把盲区标成待核实
    assert 'item._blind ? "待核实"' not in js
    assert 'blindBadge.textContent = "待核实"' not in js

    # 淡紫 token 仍在
    assert "--color-blind" in css or "#AF52DE" in css
    assert "source-badge.blind" in css or ".source-badge.blind" in css


def test_ui_verify_panel_keeps_pending_label():
    """需你确认区仍用「待核实」+ 旁注铁律。"""
    root = Path(__file__).resolve().parents[1]
    js = (root / "app/static/app.js").read_text(encoding="utf-8")
    assert 'badge.textContent = "待核实"' in js
    assert "主动核查只提疑点，不改变清单规则档" in js
    assert "需你确认" in js


# ---------- 法务五条分流 ----------

def test_triage_objection_short_must_human():
    out = verify_service.run_bounded_verify(
        text=_TEXT,
        items=[],
        quality={
            "observations": [
                {
                    "dimension": "impact",
                    "title": "异议期偏短",
                    "quote": "乙方应于收货后七日内提出书面异议。",
                    "clause_id": "c03",
                    "comment": "七日偏短，建议延长。",
                    "needs_confirm": True,
                }
            ],
            "pending_questions": [],
        },
        clause_index=_idx(),
    )
    assert out["available"] is True
    assert out["questions"], "异议期必须进人审待办"
    q = next(q for q in out["questions"] if "异议" in (q.get("title") or q.get("question") or ""))
    assert q["triage"] == "must_human"
    assert q["triage_rule"] == "objection_short"
    log = out.get("triage_log") or []
    assert any(r.get("triage_rule") == "objection_short" for r in log)


def test_triage_acceptance_annex_must_human():
    out = verify_service.run_bounded_verify(
        text=_TEXT,
        items=[],
        quality={"observations": [], "pending_questions": ["验收标准是否另附清单？"]},
        clause_index=_idx(),
    )
    assert out["questions"]
    q = out["questions"][0]
    assert "验收" in q["question"]
    assert q["triage"] == "must_human"
    assert q["triage_rule"] == "acceptance_annex"


def test_triage_payment_recheck_machine_ok_when_verified():
    idx = _idx()
    out = verify_service.run_bounded_verify(
        text=_TEXT,
        items=[
            {
                "id": "payment",
                "name": "价款与支付",
                "status": "需关注",
                "note": "付款条件需关注",
                "quote": "货款验收合格后支付",
                "primary_clause_id": "c02",
                "clause_ids": ["c02"],
            }
        ],
        quality={"observations": [], "pending_questions": []},
        blind_candidates=[],
        facts=[],
        clause_index=idx,
    )
    log = out.get("triage_log") or []
    pay = [r for r in log if r.get("triage_rule") == "payment_recheck"]
    assert pay, f"应命中价款再核对分流: {log}"
    # verified → 不进 pending
    if pay[0]["triage"] == "machine_ok":
        assert all(
            "价款" not in (q.get("title") or "") and q.get("source_ref") != "payment"
            for q in out["questions"]
        )
        assert pay[0]["verification"] == "verified"
    else:
        # 摘句若未 verified 则必须人审（五条③）
        assert pay[0]["triage"] == "must_human"
        assert pay[0]["verification"] in {"ambiguous", "missing", "unverified"}


def test_triage_payment_recheck_must_human_when_quote_missing():
    out = verify_service.run_bounded_verify(
        text=_TEXT,
        items=[
            {
                "id": "payment",
                "name": "价款与支付",
                "status": "需关注",
                "note": "付款条件需关注",
                "quote": "这段话合同里根本没有的假摘句XYZ",
                "primary_clause_id": "c02",
            }
        ],
        quality={"observations": [], "pending_questions": []},
        clause_index=_idx(),
    )
    log = out.get("triage_log") or []
    pay = next(r for r in log if r.get("triage_rule") == "payment_recheck")
    assert pay["triage"] == "must_human"
    assert pay["verification"] in {"missing", "ambiguous", "unverified"}
    assert any(q.get("source_ref") == "payment" for q in out["questions"])


def test_triage_quote_unlocated_must_human():
    out = verify_service.run_bounded_verify(
        text=_TEXT,
        items=[],
        quality={
            "observations": [
                {
                    "dimension": "consistency",
                    "title": "条款引用存疑",
                    "quote": "完全找不到的假句子ABCDEFG",
                    "clause_id": None,
                    "comment": "摘句对不上",
                    "needs_confirm": True,
                }
            ],
            "pending_questions": [],
        },
        clause_index=_idx(),
    )
    log = out.get("triage_log") or []
    assert log
    # 假摘句 → missing → quote_unlocated 优先
    hit = next(
        (r for r in log if r.get("triage_rule") == "quote_unlocated"),
        log[0],
    )
    assert hit["triage"] == "must_human"
    assert any(q["triage"] == "must_human" for q in out["questions"])


def test_triage_amount_cross_silent_when_consistent():
    text = "甲方：甲。合同总价人民币十万元整。结算同为人民币十万元整。"
    facts = [
        {
            "kind": "amount",
            "label": "金额",
            "value": "人民币十万元整",
            "evidence": build_evidence(
                text=text,
                quote="人民币十万元整",
                parse_source="fact",
                document_version="v",
            ),
        }
    ]
    # 已 verified 且有 clause 的事实会被 _collect_suspects 跳过；
    # 这里直接测 classify_triage
    sus = {
        "source": "fact",
        "source_ref": "fact:0|kind:amount",
        "title": "金额",
        "question": "请核对金额跨条款是否一致",
        "quote": "人民币十万元整",
    }
    disp, rule, reason = verify_service.classify_triage(
        sus,
        verification="verified",
        fact_ok=True,
        text=text,
        facts=facts,
    )
    assert disp == "machine_silent"
    assert rule == "amount_cross"
    assert "一致" in reason or "静默" in reason


def test_triage_amount_cross_must_human_when_mismatch():
    text = _TEXT_AMOUNT_MISMATCH
    facts = [
        {
            "kind": "amount",
            "label": "金额",
            "value": "人民币十万元整",
            "evidence": build_evidence(
                text=text, quote="人民币十万元整", parse_source="fact", document_version="v"
            ),
        },
        {
            "kind": "amount",
            "label": "金额",
            "value": "人民币八万元整",
            "evidence": build_evidence(
                text=text, quote="人民币八万元整", parse_source="fact", document_version="v"
            ),
        },
    ]
    sus = {
        "source": "fact",
        "source_ref": "fact:0|kind:amount",
        "title": "金额",
        "question": "请核对金额跨条款是否一致",
        "quote": "人民币十万元整",
    }
    disp, rule, reason = verify_service.classify_triage(
        sus,
        verification="verified",
        fact_ok=True,
        text=text,
        facts=facts,
    )
    assert disp == "must_human"
    assert rule == "amount_cross"


def test_triage_never_rewrites_rule_status():
    """分流只影响 verify 待办，绝不改规则四档。"""
    items = [
        {
            "id": "payment",
            "name": "价款与支付",
            "status": "需关注",
            "quote": "货款验收合格后支付",
            "primary_clause_id": "c02",
        },
        {"id": "party", "name": "主体", "status": "通过", "quote": "甲方：某某科技有限公司"},
    ]
    snap = [(i["id"], i["status"]) for i in items]
    out = verify_service.run_bounded_verify(
        text=_TEXT,
        items=items,
        quality={
            "observations": [
                {
                    "title": "异议期偏短",
                    "quote": "乙方应于收货后七日内提出书面异议。",
                    "comment": "偏短",
                    "needs_confirm": True,
                }
            ],
            "pending_questions": ["验收标准是否另附？"],
        },
        clause_index=_idx(),
    )
    assert [(i["id"], i["status"]) for i in items] == snap
    blob = str(out)
    assert "改判" not in blob
