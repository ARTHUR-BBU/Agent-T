"""背靠背付款条款 hardline 簇（路线图阶段 0.3，老钱裁决）。

背景：法释〔2024〕11号——大型企业与中小企业约定以第三方支付款项为
付款前提的条款无效。此前三品类均逮不住，且「验收合格.{0,10}支付」
pass 词表会把背靠背条款洗成「通过」（最危险结局）。
"""
from __future__ import annotations

from app.services.checklist import STATUS_ATTENTION, STATUS_PASS, run_checklist


def _payment_status(text: str) -> dict:
    result = run_checklist(text, category="procurement")
    return next(i for i in result["items"] if i["id"] == "payment")


def test_qian_example_backtoback_caught():
    """老钱裁决书原句：收不到最终客户款项则不付服务费。"""
    text = (
        "甲方在收到最终客户支付的款项后，向乙方支付服务费。"
        "合同总价人民币10万元，分二期支付。"
    )
    item = _payment_status(text)
    assert item["status"] == STATUS_ATTENTION
    assert "法释〔2024〕11号" in item["note"]
    assert "背靠背" in item["note"]


def test_bare_backtoback_keyword_caught():
    text = "本合同付款采用背靠背方式，甲方收到业主付款后再向乙方支付。"
    item = _payment_status(text)
    assert item["status"] == STATUS_ATTENTION


def test_ye_zhu_prerequisite_caught():
    text = "甲方以业主拨款为付款前提，向乙方支付合同价款。"
    item = _payment_status(text)
    assert item["status"] == STATUS_ATTENTION


def test_normal_acceptance_payment_not_flagged():
    """正常的「验收合格后支付」是买方保护，不得误伤。"""
    text = "货物经甲方验收合格后，甲方在10日内向乙方支付全部货款。"
    item = _payment_status(text)
    assert item["status"] != STATUS_ATTENTION


def test_backtoback_not_washed_by_pass_clause():
    """核心回归（老钱点名的洗白场景）：合同同时含正常验收付款与背靠背，
    不得因 pass 词表命中而放行背靠背条款。"""
    text = (
        "首期款：货物经甲方验收合格后支付合同总价的50%。"
        "尾期款：甲方在收到最终客户支付的款项后，向乙方支付剩余50%货款。"
    )
    item = _payment_status(text)
    assert item["status"] == STATUS_ATTENTION, "背靠背条款不得被验收付款表述洗成通过"
    assert item["quote"], "必须给出背靠背条款原文定位"


# ---------- 小智娘门禁对抗探针（2026-09-09，P2-1 误报三连） ----------

def test_fp_ye_zhu_approval_schedule_not_flagged():
    """业主批复的支付进度为准 = 审批节奏，不是第三方付款前提。"""
    text = "进度款以业主批复的支付进度为准，验收合格后甲方分批支付。"
    assert _payment_status(text)["status"] != STATUS_ATTENTION


def test_fp_third_party_payment_platform_not_flagged():
    """第三方支付平台是支付渠道，不是付款前提。"""
    text = "货款以第三方支付平台到账为准，到账后3日内支付。"
    assert _payment_status(text)["status"] != STATUS_ATTENTION


def test_fp_negated_backtoback_not_flagged():
    """明示排除背靠背的保护条款反被标 = 荒谬。"""
    text = "本合同不采用背靠背付款方式，甲方应于验收合格后10日内支付全部货款。"
    assert _payment_status(text)["status"] != STATUS_ATTENTION


# ---------- 小智娘门禁漏报探针（P2-2，建筑/分包高频直陈句式） ----------

def test_fn_ye_zhu_funds_arrival_caught():
    text = "业主资金到账后，甲方按同等比例向乙方支付进度款。"
    assert _payment_status(text)["status"] == STATUS_ATTENTION


def test_fn_ye_zhu_payment_in_place_caught():
    text = "业主付款到位后，甲方再向乙方支付。"
    assert _payment_status(text)["status"] == STATUS_ATTENTION


def test_fn_settlement_with_final_client_caught():
    text = "与最终客户结算完成后支付。"
    assert _payment_status(text)["status"] == STATUS_ATTENTION


def test_fn_upstream_settlement_caught():
    text = "待上游结算后再与乙方结算。"
    assert _payment_status(text)["status"] == STATUS_ATTENTION


# ---------- 小智娘门禁 P2-3：hardline note 不得被泛化规则抢走 ----------

def test_hardline_note_wins_over_general_rule():
    """同句同时命中全款簇与背靠背簇：note 必须是法条+双面性（hardline 置顶）。"""
    text = "甲方收到最终客户款项后向乙方支付全款。"
    item = _payment_status(text)
    assert item["status"] == STATUS_ATTENTION
    assert "法释〔2024〕11号" in item["note"], "hardline 法条 note 被泛化规则覆盖"
