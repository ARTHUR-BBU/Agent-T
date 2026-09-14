"""可信度 A4：立场进入 quality + ask 解释；规则档位立场无关（Design B）。"""
from __future__ import annotations

from pathlib import Path

from app.prompts import quality as quality_prompts
from app.services import llm_ask, stance as stance_service
from app.services.checklist import run_checklist

ROOT = Path(__file__).resolve().parents[1]
PROC = ROOT / "fixtures" / "procurement_sample.txt"
NDA = ROOT / "fixtures" / "nda_gold_risks.txt"


def _statuses(category: str, text: str) -> dict[str, str]:
    return {it["id"]: it["status"] for it in run_checklist(text, category)["items"]}


def test_design_b_rules_identical_across_stances_procurement():
    """同一合同、不同立场 → 规则 checklist 档位完全一致。"""
    text = PROC.read_text(encoding="utf-8")
    base = _statuses("procurement", text)
    # stance 根本不进 run_checklist；重复跑两次证明确定性，并与「立场无关」语义对齐
    assert _statuses("procurement", text) == base
    assert base, "采购清单不得为空"


def test_design_b_rules_identical_across_stances_nda():
    """NDA：披露方 / 接收方 / 中性 均不改变规则档位（档位由尺子签发）。"""
    path = NDA if NDA.exists() else PROC
    text = path.read_text(encoding="utf-8")
    # run_checklist 无 stance 参数——显式钉死 API 形状（Design B）
    import inspect

    sig = inspect.signature(run_checklist)
    assert "stance" not in sig.parameters
    a = _statuses("nda", text)
    b = _statuses("nda", text)
    assert a == b and a


def test_quality_prompts_include_stance_field():
    system = quality_prompts.build_system_prompt(
        ["诚信原则"], category="nda", stance="disclosing"
    )
    user = quality_prompts.build_user_prompt(
        "合同正文。", [], None, category="nda", stance="disclosing"
    )
    assert "【用户声明立场】披露方" in system
    assert "规则引擎档位或核查口径" in system or "规则清单与立场无关" in system
    assert "谁受益" in system and "谁承担义务" in system
    assert "用户声明立场：披露方" in user
    # 不得暗示已按立场改规则
    assert "已按立场个性化" not in system
    assert "已按立场改规则" not in system


def test_quality_prompts_nda_receiving_phrasing():
    system = quality_prompts.build_system_prompt([], category="nda", stance="receiving")
    assert "接收方" in system
    assert "披露方" in system
    assert "保密义务" in system


def test_ask_prompts_include_stance_field():
    system = llm_ask._build_system_prompt(
        ["先验收后付款"], category="nda", stance="receiving"
    )
    item = {
        "name": "保密义务",
        "id": "scope",
        "status": "需关注",
        "note": "过宽",
        "quote": "一切信息均属保密",
    }
    user = llm_ask._build_user_prompt(
        "对我方有何影响？",
        item,
        "一切信息均属保密。",
        category="nda",
        stance="receiving",
    )
    assert "【用户声明立场】接收方" in system
    assert "谁受益" in system and "谁承担义务" in system
    assert "用户声明立场：接收方" in user
    assert "NDA 接收方视角" in system


def test_ask_prompts_nda_disclosing_phrasing():
    system = llm_ask._build_system_prompt([], category="nda", stance="disclosing")
    assert "披露方" in system
    assert "接收方" in system
    assert "保密义务" in system


def test_stance_prompt_guidance_single_source():
    """quality / ask 共用 stance.prompt_guidance，避免平行立场系统。"""
    block = stance_service.prompt_guidance("nda", "disclosing")
    q = quality_prompts.build_system_prompt([], category="nda", stance="disclosing")
    a = llm_ask._build_system_prompt([], category="nda", stance="disclosing")
    assert block in q and block in a


def test_quality_run_passes_stance_into_chat(monkeypatch):
    """run_quality(..., stance=) 必须把立场写进实际送模 prompt。"""
    monkeypatch.setenv("QUALITY_ENABLED", "true")
    from app.services import quality as quality_service

    seen: list[tuple[str, str]] = []

    def chat(system: str, user: str) -> str:
        seen.append((system, user))
        return '{"observations":[],"facts":[],"pending_questions":[]}'

    out = quality_service.run_quality(
        text="甲方：甲。乙方：乙。合同总价一万元。适用中华人民共和国法律。",
        items=[],
        category="nda",
        stance="receiving",
        chat_fn=chat,
    )
    assert out["available"] is True
    assert seen, "必须实际调用 chat"
    system, user = seen[0]
    assert "接收方" in system and "用户声明立场：接收方" in user


def test_ui_stance_hint_does_not_imply_personalized_rules():
    """上传页提示不得暗示「已按立场个性化改规则」。"""
    js = (ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")
    assert "规则清单不变" in js or "规则核查照旧" in js
    assert "已按立场个性化" not in js
    assert "仅 AI 解释会点明谁受益" in js
