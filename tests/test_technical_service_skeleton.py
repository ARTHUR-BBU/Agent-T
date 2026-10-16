# -*- coding: utf-8 -*-
"""B2-1 技术服务品类骨架 · 对抗样例集（验收单 docs/b2-acceptance-matrix.md）。

13 项清单关键场景覆盖；每条 hardline ≥2 条对抗（换措辞仍命中+反例不得命中）。
功能开关（v1.1.1）：technical_service 对用户不可见（hidden），引擎测试入口照常。
"""
from __future__ import annotations

import pytest

from app.services import checklist


def _item(text: str, item_id: str) -> dict:
    out = checklist.run_checklist(text, category="technical_service")
    return next(i for i in out["items"] if i["id"] == item_id)


# ---------- 功能开关（v1.1.1 外审复批） ----------

def test_hidden_category_not_visible_to_users() -> None:
    """hidden 品类对用户不可见（UI 数据源），但测试入口 include_hidden 可见。"""
    visible = [c["id"] for c in checklist.list_categories()]
    assert "technical_service" not in visible, "功能开关泄漏：hidden 品类对用户可见"
    all_cats = [c["id"] for c in checklist.list_categories(include_hidden=True)]
    assert "technical_service" in all_cats


def test_engine_entry_works_behind_flag() -> None:
    """引擎测试入口照常（13 项全量）。"""
    out = checklist.run_checklist("乙方应开发系统。", category="technical_service")
    assert len(out["items"]) == 13


def test_hidden_bad_config_still_fails_startup(tmp_path, monkeypatch) -> None:
    """外审 #108 P2 回归钉死：hidden 品类必须同样过启动校验——
    若 validate 退回排除 hidden（功能开关回归），坏正则 hidden 配置
    仍须炸响。现有坏配置 fixture 全是可见品类，本条补盲区。"""
    import yaml

    from app.services.checklist import validate_checklist_configs

    bad = {
        "category": "hidden_bad",
        "label": "隐藏坏配置",
        "hidden": True,
        "items": [
            {
                "id": "x",
                "name": "x",
                "segment": "A",
                "rules": {"need_attention": [{"any_of": ["(未闭合括号"], "note": "n"}]},
            }
        ],
    }
    (tmp_path / "checklist_hidden_bad.yaml").write_text(
        yaml.safe_dump(bad, allow_unicode=True), encoding="utf-8")
    monkeypatch.setattr(checklist, "CONFIG_DIR", tmp_path)
    with pytest.raises(ValueError, match="非法正则"):
        validate_checklist_configs()


# ---------- 2. service_scope ----------

def test_scope_vague_externalized_standard() -> None:
    assert _item("服务内容按乙方标准执行。", "service_scope")["status"] == "需关注"


def test_scope_function_and_deliverable_pass() -> None:
    item = _item("功能模块包括用户管理、订单管理；交付物含源代码与部署文档。", "service_scope")
    assert item["status"] == "通过"


def test_scope_no_source_code_attention() -> None:
    """源码缺失（偏向委托方注记；换措辞仍命中）。"""
    for t in ("本项目不交付源代码。", "乙方仅交付目标程序。"):
        assert _item(t, "service_scope")["status"] == "需关注", t


# ---------- 3. acceptance（头号项，hardline 双向） ----------

@pytest.mark.parametrize("text", [
    "验收标准以乙方交付为准。",
    "系统验收由乙方自行验收。",
    "乙方须无条件通过验收。",
    "免费重新开发直至甲方满意。",
    "验收不合格的，乙方仅负责修复，不承担任何违约责任。",
])
def test_acceptance_hardline_variants(text: str) -> None:
    assert _item(text, "acceptance")["status"] == "需关注", text


def test_acceptance_default_on_launch() -> None:
    """默认验收（擅自使用视为认可，对委托方不利）。"""
    assert _item("系统上线即视为验收合格。", "acceptance")["status"] == "需关注"


@pytest.mark.parametrize(("text", "exp"), [
    # 外审 P2 #107 短异议期精确矩阵：长数字个位不误、中文/数字短日期命中
    ("验收标准：按照需求规格说明书测试用例验收。甲方应在交付后11日内提出异议，逾期视为验收合格。", "通过"),
    ("验收标准符合国家标准。甲方应在交付后13日内提出异议，逾期视为验收合格。", "通过"),
    ("验收标准：按照需求规格说明书测试用例验收。甲方应在交付后2日内提出异议，逾期视为验收合格。", "需关注"),
    ("验收标准符合国家标准。甲方应在交付后三日内未提出异议视为验收合格。", "需关注"),
    ("验收标准符合国家标准 GB/T 25000。甲方收到交付物后10日内未提出异议视为验收合格。", "通过"),
])
def test_acceptance_short_review_period_precision(text: str, exp: str) -> None:
    assert _item(text, "acceptance")["status"] == exp, text


def test_acceptance_substantive_standard_pass() -> None:
    """实质锚定：验收∧标准词同句；「验收合格后付款」的验收不算本项证据。"""
    assert _item("验收标准：符合国家标准 GB/T 25000。", "acceptance")["status"] == "通过"
    assert _item("按需求规格说明书测试用例进行验收。", "acceptance")["status"] == "通过"


def test_acceptance_payment_context_not_pass_evidence() -> None:
    """跨项语义域纪律：「验收合格后付款」不构成本项 pass 证据。"""
    assert _item("项目验收合格后付款。", "acceptance")["status"] == "需关注"


def test_acceptance_missing_is_attention() -> None:
    """missing_as 需关注（核心条款缺失，民法典878条脉络）。"""
    assert _item("本合同一式两份。", "acceptance")["status"] == "需关注"


# ---------- 4. schedule（procurement_term 四分类移植） ----------

def test_schedule_anchored_pass() -> None:
    # 用 matcher 词表内标签（「开发周期」扩词挂 B2-2 语料校准，避免共享
    # matcher 词表改动波及 17 份采购对照）
    assert _item("服务期自2026年1月1日起至2026年6月30日止。", "schedule")["status"] == "通过"


def test_schedule_pending_attention() -> None:
    assert _item("工期：另行协商。", "schedule")["status"] == "需关注"


def test_schedule_unilateral_extension() -> None:
    assert _item("乙方有权单方顺延工期。", "schedule")["status"] == "需关注"


# ---------- 5. payment（背靠背移植+技术特有） ----------

def test_payment_back_to_back_hardline() -> None:
    """背靠背：主形态+换措辞变体都拦（法释〔2024〕11号含服务；note 有限定）。"""
    for t in (
        "以甲方从最终客户处收到全部款项作为乙方付款前提。",
        "甲方收到业主回款后，再向乙方支付相应款项，以此为准。",
    ):
        item = _item(t, "payment")
        assert item["status"] == "需关注", t
        assert "大型企业与中小企业" in (item.get("note") or ""), "note 适用范围限定缺失"


def test_payment_back_to_back_negation_pass() -> None:
    assert _item("双方确认不采用背靠背结算模式。", "payment")["status"] != "需关注"


def test_payment_tail_linked_to_open_acceptance() -> None:
    """技术特有：尾款系于验收但全文无验收标准→付款条件可无限不成就。"""
    assert _item("验收合格后支付全部尾款。", "payment")["status"] == "需关注"


# ---------- 6. breach（F-2 移植） ----------

def test_breach_f2_mutual_force_majeure_pass() -> None:
    """对等量词+免责事由同句→豁免（F-2 门）。"""
    item = _item("任何一方对由于不可抗力造成的部分或全部不能履行合同不承担违约责任。", "breach")
    assert item["status"] != "需关注"


def test_breach_one_sided_exemption_blocked() -> None:
    """真单方写法照旧必中（F-2 门不放行）。"""
    assert _item("乙方逾期交付的，免除乙方全部违约责任。", "breach")["status"] == "需关注"


def test_breach_one_sided_penalty_no_reciprocal() -> None:
    """外审 P2 #107：单向罚则（乙方逾期计罚∧甲方逾期无对等责任）→需关注，
    不得被宽 pass「违约金」洗成通过。"""
    t = "乙方逾期交付的，每日按合同总价的千分之五支付违约金。"
    assert _item(t, "breach")["status"] == "需关注"


def test_breach_reciprocal_penalty_pass() -> None:
    """对等罚则（甲方逾期付款责任在场）→通过（matcher 双向判定）。"""
    t = ("乙方逾期交付的，每日按合同总价的千分之五支付违约金；"
         "甲方逾期付款的，每逾期一日按应付款项的千分之一支付违约金。")
    assert _item(t, "breach")["status"] == "通过"


# ---- 外审 #107 二轮 2 P1 固化 ----

def test_payment_tail_with_standard_gives_pass() -> None:
    """P1-1a：safeguard（验收标准）放行+尾款 pass 路径=通过（不再未找到）。"""
    t = "验收标准：按照需求规格说明书测试用例验收。验收合格后支付全部尾款。"
    assert _item(t, "payment")["status"] == "通过"


def test_payment_tail_with_deadline_gives_pass() -> None:
    """P1-1b：safeguard（验收期限，数字与「日」间有空格形态）→通过。"""
    t = "甲方应在交付后 10 日内完成验收。验收合格后支付全部尾款。"
    assert _item(t, "payment")["status"] == "通过"


def test_payment_bare_tail_still_attention() -> None:
    """无 safeguard 裸尾款仍拦（need_attention 先于 pass）。"""
    assert _item("验收合格后支付全部尾款。", "payment")["status"] == "需关注"


def test_breach_vendee_role_one_sided() -> None:
    """P1-2a：受托方角色词族单向罚则（无甲方/乙方字样）也命中 matcher。"""
    assert _item("受托方逾期交付，支付违约金。", "breach")["status"] == "需关注"


def test_breach_vendee_reciprocal_pass() -> None:
    """P1-2b：受托方罚则+委托方逾期付款罚则=对等→通过。"""
    t = "受托方逾期交付，支付违约金。委托方逾期付款，支付违约金。"
    assert _item(t, "breach")["status"] == "通过"


def test_breach_irrelevant_counterparty_duty_not_reciprocal() -> None:
    """P1-2c：委托方仅有无关责任（逾期提供资料）不构成付款对等——仍需关注。"""
    t = "受托方逾期交付，支付违约金。委托方逾期提供需求资料的，顺延工期。"
    assert _item(t, "breach")["status"] == "需关注"


def test_breach_service_party_roles_pass() -> None:
    """P1-2d：服务方/客户方角色词族对等→通过。"""
    t = "服务方逾期交付，支付违约金。客户方逾期付款，支付违约金。"
    assert _item(t, "breach")["status"] == "通过"


# ---------- 7. termination（v1.1.1 中性说明） ----------

def test_termination_present_pass() -> None:
    assert _item("任何一方可提前30日书面通知解除本合同。", "termination")["status"] == "通过"


def test_termination_missing_attention_neutral_note() -> None:
    """missing_as 需关注+B2-1 中性说明（不展示 857/858 技术开发语境）。"""
    item = _item("本合同一式两份。", "termination")
    assert item["status"] == "需关注"
    assert "857" not in (item.get("note") or ""), "B2-1 subtype=unknown 不得展示技术开发法律语境"


# ---------- 8. ip（方向 hardline+中性 missing） ----------

def test_ip_missing_neutral_wording() -> None:
    """missing_as 需关注，中性口径（不落「沉默即失权」）。"""
    item = _item("本合同一式两份。", "ip")
    assert item["status"] == "需关注"
    note = item.get("note") or ""
    assert "失权" not in note, "v1.1 不得使用「沉默即失权」表述"
    assert "859" in note or "未明确约定" in note


def test_ip_all_to_vendee_hardline() -> None:
    assert _item("本合同项下一切开发成果的知识产权均归受托方所有。", "ip")["status"] == "需关注"


def test_ip_all_to_client_swallows_prior_hardline() -> None:
    """吞并既有成果（反向红线，偏向甲方）。"""
    t = "开发形成的一切成果均归甲方所有，包括乙方既有成果。"
    assert _item(t, "ip")["status"] == "需关注"


def test_ip_ownership_clause_pass() -> None:
    """归属语义域：裸提及不算，「X归Y」才算。"""
    assert _item("本项目技术成果知识产权归甲方所有，背景知识产权仍归各方。", "ip")["status"] == "通过"


def test_ip_bare_mention_not_pass() -> None:
    """裸提及（不得侵犯第三方知产）不得通过（金标 v2 口径）。"""
    assert _item("双方均不得侵犯第三方知识产权。", "ip")["status"] != "通过"


# ---------- 9. unfair_terms（双向 hardline） ----------

@pytest.mark.parametrize("text", [
    "服务质量问题按行业惯例执行。",
    "乙方不对开发成果的可用性作任何保证。",
    "甲方数据丢失的，乙方不承担责任。",
    "系统提供永久免费维护。",
    "需求变更无条件纳入范围且不加价。",
])
def test_unfair_hardline_variants(text: str) -> None:
    assert _item(text, "unfair_terms")["status"] == "需关注", text


def test_unfair_industry_practice_with_anchor_not_hit() -> None:
    """反例：「按行业惯例及国家标准执行」有客观锚点，不得被裸词连坐。"""
    item = _item("服务质量按行业惯例及国家标准执行。", "unfair_terms")
    assert item["status"] != "需关注"


# ---------- 10. confidentiality（四态 day one+制式陷阱） ----------

def test_tisvc_blank_template_not_pass() -> None:
    """本品类头号陷阱：制式空模板「保密义务如下：」不得洗成通过（80% 语料形态）。"""
    item = _item("第五条 双方确定因履行本合同应遵守的保密义务如下：\n1．保密内容（包括技术信息和经营信息）：\n2．涉密人员范围：\n3．保密期限：\n4．泄密责任：", "confidentiality")
    assert item["status"] != "通过"


def test_tisvc_subitem_filled_pass() -> None:
    """制式子项填写实质内容→通过（冒号同格）。"""
    t = ("第五条 双方确定因履行本合同应遵守的保密义务如下：\n"
         "1．保密内容（包括技术信息和经营信息）：双方因履行本合同知悉的技术资料和商业信息。\n"
         "2．涉密人员范围：双方项目参与人员。\n"
         "3．保密期限：合同终止后五年。\n"
         "4．泄密责任：违约方赔偿对方经济损失。")
    assert _item(t, "confidentiality")["status"] == "通过"


def test_tisvc_subitem_tab_delimited_pass() -> None:
    """表格跨格 tab 形态（sjtu 表格版式）。"""
    assert _item("保密内容\t双方知悉的技术资料", "confidentiality")["status"] == "通过"


def test_tisvc_denied_wu_attention() -> None:
    """官方否认通道：填「无」→需关注（不升 hardline，与采购同档）。"""
    assert _item("保密义务如下：无。", "confidentiality")["status"] == "需关注"


def test_tisvc_blank_flag_on_empty_subitems() -> None:
    """留白：子项冒号/tab 后空白→旗标。"""
    item = _item("保密内容（包括技术信息和经营信息）：\n涉密人员范围：\n", "confidentiality")
    assert item["status"] == "需关注"
    assert item.get("blank_flag") is True


def test_tisvc_missing_is_attention() -> None:
    """sjtu 表格版形态：通篇无保密→missing_as 需关注（845条+接触深度）。"""
    assert _item("本合同一式两份。", "confidentiality")["status"] == "需关注"


def test_tisvc_substantive_pass() -> None:
    """实质义务照常通过。"""
    assert _item("双方对知悉的商业秘密负有保密义务，不得向第三方披露。", "confidentiality")["status"] == "通过"


# ---------- 12/13. governing_law / signature（移植抽测） ----------

def test_governing_law_missing_attention() -> None:
    assert _item("争议提交北京仲裁委员会仲裁。", "governing_law")["status"] == "需关注"


def test_signature_stamp_only_attention() -> None:
    assert _item("甲方（盖章）：\n乙方（盖章）：", "signature")["status"] == "需关注"


def test_signature_with_representative_pass() -> None:
    assert _item("甲方（盖章），授权代表签字：张三。", "signature")["status"] == "通过"
