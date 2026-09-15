"""异议层（阶段 3.1/3.2）单元测试：受理分流 + 五要件 + 铁律 3 支柱。

防线分层（与 test_quality.py 注入对抗同构）：
① 伪造 quote → 全文校验硬拒收；② 反证原文核验；③ legal_reasoning ≥30 字；
④ 立场自检；⑤ 方向×类别匹配（heuristic 只收误报/existence 只收漏报/
hardline 不送审）；⑥ needs_confirm 代码强制。
铁律 3：异议开/关两次 items+scorecard 深度相等（test_objection_api.py）。
"""
from __future__ import annotations

import json

import pytest

from app.prompts.guards import UNTRUSTED_DOCUMENT_INSTRUCTION
from app.prompts import objection as objection_prompts
from app.services import objection as objection_service
from app.services.clause_index import build_clause_index
from app.services.llm_budget import ReviewBudget

pytestmark = pytest.mark.usefixtures("_objections_off")

_CONTRACT = (
    "甲方：某公司。乙方：某供应商。\n"
    "第一条 标的：办公设备一批，明细见附件。\n"
    "第二条 付款：货款十万元，验收合格后一次性支付。\n"
    "第三条 转租：乙方不得转租，违反的出租方可解除合同。\n"
    "第四条 保密：乙方对合作内容负有保密义务。\n"
    "本合同一式两份，自双方签字盖章之日起生效。\n"
)

_ITEMS = [
    {  # heuristic + 需关注 → 误报候选
        "id": "sublet", "name": "转租限制", "status": "需关注",
        "note": "转租禁止叠加解除", "quote": "乙方不得转租，违反的出租方可解除合同",
        "hits": ["转租"], "rule_id": "sublet#r0", "rule_class": "heuristic",
        "clause_ids": ["c03"], "primary_clause_id": "c03",
    },
    {  # hardline + 需关注 → 不送审
        "id": "deposit", "name": "押金", "status": "需关注",
        "note": "押金没收", "quote": "押金不予退还", "hits": ["押金"],
        "rule_id": "deposit#r0", "rule_class": "hardline",
        "clause_ids": ["c02"], "primary_clause_id": "c02",
    },
    {  # existence + 未找到 → 漏报候选
        "id": "governing_law", "name": "适用法律", "status": "未找到",
        "note": "未找到适用法律", "quote": "", "hits": [],
        "rule_id": None, "rule_class": "existence",
        "clause_ids": [], "primary_clause_id": None,
    },
]


def _enable(monkeypatch):
    monkeypatch.setenv("OBJECTIONS_ENABLED", "true")


def _ok_payload(**overrides):
    base = {
        "item_id": "sublet",
        "rule_id": "sublet#r0",
        "direction": "false_positive",
        "quote": "第三条 转租：乙方不得转租，违反的出租方可解除合同。",
        "counter_evidence": "未发现反证原文",
        "legal_reasoning": "民法典第七百一十六条赋予承租人经同意转租的法定默认安排，"
                           "仅约定「不得转租」并附解除权属于正常权利配置，需结合是否"
                           "实际同意判断，不必然构成真实风险。",
        "stance_check": "与立场无关",
        "proposal": "为「转租」词表增加「经出租方书面同意」反证 unless",
    }
    base.update(overrides)
    return base


def _payload(objs):
    return json.dumps({"objections": objs}, ensure_ascii=False)


def test_disabled_by_default(monkeypatch):
    monkeypatch.setenv("OBJECTIONS_ENABLED", "false")
    calls = []
    out = objection_service.run_objections(
        text=_CONTRACT, items=_ITEMS,
        chat_fn=lambda s, u: (calls.append(1) or _payload([_ok_payload()])),
    )
    assert out["reason"] == "disabled" and not calls


def test_hardline_never_sent(monkeypatch):
    """hardline 簇分流拦截：候选块里不得出现 deposit。"""
    _enable(monkeypatch)
    captured = {}

    def chat(system, user):
        captured["user"] = user
        return _payload([])

    out = objection_service.run_objections(
        text=_CONTRACT, items=_ITEMS, chat_fn=chat,
    )
    assert "deposit" not in captured["user"], "hardline 簇不得送审"
    assert "sublet" in captured["user"] and "governing_law" in captured["user"]


def test_five_requirements_all_pass_accepted(monkeypatch):
    _enable(monkeypatch)
    out = objection_service.run_objections(
        text=_CONTRACT, items=_ITEMS, chat_fn=lambda s, u: _payload([_ok_payload()]),
        clause_index=build_clause_index(_CONTRACT),
    )
    assert out["available"] is True
    assert len(out["objections"]) == 1
    o = out["objections"][0]
    assert o["accepted"] is True and o["needs_confirm"] is True
    assert o["clause_id"] == "c03", "服务端定位派生条款归属"


def test_missing_quote_rejected(monkeypatch):
    _enable(monkeypatch)
    out = objection_service.run_objections(
        text=_CONTRACT, items=_ITEMS,
        chat_fn=lambda s, u: _payload([_ok_payload(quote="这句话不在合同里")]),
    )
    assert out["rejected_count"] == 1
    assert out["objections"][0]["accepted"] is False
    assert "要件①" in (out["objections"][0]["reject_reason"] or "")


def test_counter_evidence_unverifiable_rejected(monkeypatch):
    _enable(monkeypatch)
    out = objection_service.run_objections(
        text=_CONTRACT, items=_ITEMS,
        chat_fn=lambda s, u: _payload([
            _ok_payload(counter_evidence="乙方曾书面同意转租（不在本合同中）"),
        ]),
    )
    assert out["rejected_count"] == 1
    assert out["objections"][0]["accepted"] is False


def test_short_reasoning_rejected(monkeypatch):
    _enable(monkeypatch)
    out = objection_service.run_objections(
        text=_CONTRACT, items=_ITEMS,
        chat_fn=lambda s, u: _payload([_ok_payload(legal_reasoning="我觉得没问题。")]),
    )
    assert out["rejected_count"] == 1
    assert out["objections"][0]["accepted"] is False


def test_stance_mismatch_rejected(monkeypatch):
    _enable(monkeypatch)
    out = objection_service.run_objections(
        text=_CONTRACT, items=_ITEMS, stance="lessee",
        chat_fn=lambda s, u: _payload([_ok_payload(stance_check="出租方")]),
    )
    assert out["rejected_count"] == 1
    assert out["objections"][0]["accepted"] is False


def test_direction_class_mismatch_rejected(monkeypatch):
    """heuristic 簇只收误报；模型越类申报 omission → 直接丢弃不留痕
    （越类申报是模型结构性错误，非可展示的候选）。"""
    _enable(monkeypatch)
    out = objection_service.run_objections(
        text=_CONTRACT, items=_ITEMS,
        chat_fn=lambda s, u: _payload([_ok_payload(direction="omission")]),
    )
    assert out["rejected_count"] == 1 and out["objections"] == []


def test_omission_for_existence_accepted(monkeypatch):
    """existence 未找到项的漏报异议走通。"""
    _enable(monkeypatch)
    obj = _ok_payload(
        item_id="governing_law", rule_id=None, direction="omission",
        quote="第四条 保密：乙方对合作内容负有保密义务。",
        legal_reasoning="合同已含保密安排但未写适用法律；本条用于验证漏报异议"
                        "通道：等价写法被词表漏掉时应受理漏报候选。",
    )
    out = objection_service.run_objections(
        text=_CONTRACT, items=_ITEMS,
        chat_fn=lambda s, u: _payload([obj]),
        clause_index=build_clause_index(_CONTRACT),
    )
    assert out["available"] is True
    assert out["objections"] and out["objections"][0]["direction"] == "omission"


def test_forbidden_phrase_triggers_retry_then_clean_or_fail(monkeypatch):
    _enable(monkeypatch)
    bad = _ok_payload(legal_reasoning="该条并无风险，建议直接通过。" + "理由" * 20)
    good = _ok_payload()
    rounds = {"n": 0}

    def chat(system, user):
        rounds["n"] += 1
        return _payload([bad]) if rounds["n"] == 1 else _payload([good])

    out = objection_service.run_objections(text=_CONTRACT, items=_ITEMS, chat_fn=chat)
    print("DEBUG out:", json.dumps(out, ensure_ascii=False)[:400])
    assert rounds["n"] == 2, "禁语命中恰好重试一次"
    assert out["available"] is True


def test_budget_exhausted_soft_degrades(monkeypatch):
    _enable(monkeypatch)
    budget = ReviewBudget(1)
    budget.try_consume()
    out = objection_service.run_objections(
        text=_CONTRACT, items=_ITEMS,
        chat_fn=lambda s, u: _payload([_ok_payload()]), budget=budget,
    )
    assert out["reason"] == "budget_exceeded"


def test_no_candidates_returns_empty_ok(monkeypatch):
    """全 hardline/通过：合法空结果（不烧 LLM）。"""
    _enable(monkeypatch)
    calls = []
    items = [{"id": "d", "name": "押金", "status": "需关注", "rule_class": "hardline"}]
    out = objection_service.run_objections(
        text=_CONTRACT, items=items,
        chat_fn=lambda s, u: (calls.append(1) or _payload([])),
    )
    assert out["available"] is True and out["objections"] == [] and not calls


def test_system_prompt_mounts_guard():
    system = objection_prompts.build_system_prompt("neutral")
    assert UNTRUSTED_DOCUMENT_INSTRUCTION in system


def test_needs_confirm_forced(monkeypatch):
    _enable(monkeypatch)
    out = objection_service.run_objections(
        text=_CONTRACT, items=_ITEMS,
        chat_fn=lambda s, u: _payload([_ok_payload(needs_confirm=False)]),
        clause_index=build_clause_index(_CONTRACT),
    )
    assert out["objections"][0]["needs_confirm"] is True
