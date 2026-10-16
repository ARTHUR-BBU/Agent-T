# -*- coding: utf-8 -*-
"""B2-2 技术服务语料金标锁定（老钱签认文书 2026-10-16：52 签认+13 改判）。

65 格直锁（5 份×13 项）——规则迭代若翻格：测试红→PR 必须附翻格前后
65 格 diff+法务理由，金标修订需老钱签认后方可改断言（不允许静默重算）。
语料=fixtures-tisvc/（catdoc 生产同构提取，双 SHA256 见 MANIFEST.json）。

3 陷阱专项：tisvc-most 保密跨行形态（需关注+blank_flag 双断言）、
payment 空白总额不得通过、breach 示范括注不得通过。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.services import checklist

ROOT = Path(__file__).resolve().parent.parent

ITEMS = [
    "subject", "service_scope", "schedule", "payment", "acceptance",
    "breach", "termination", "ip", "unfair_terms", "confidentiality",
    "jurisdiction", "governing_law", "signature",
]

# 老钱签认终态矩阵（2026-10-16 文书：52 签认+13 改判——主体×5/付款×3/违约×5，
# 改判全部是「空白栏目被示范文字洗成通过」同款病）
GOLD_MATRIX = {
    "techdev-coop-most-2001": ["需关注", "未找到", "未找到", "未找到", "需关注", "未找到", "通过", "需关注", "未找到", "需关注", "通过", "需关注", "通过"],
    "techdev-most-2001": ["需关注", "未找到", "未找到", "未找到", "需关注", "未找到", "通过", "需关注", "通过", "需关注", "通过", "需关注", "通过"],
    "tisvc-most-2001": ["需关注", "未找到", "未找到", "未找到", "需关注", "未找到", "通过", "需关注", "通过", "需关注", "通过", "需关注", "通过"],
    "tisvc-shgov-2020": ["需关注", "未找到", "未找到", "未找到", "需关注", "未找到", "通过", "需关注", "通过", "需关注", "通过", "需关注", "通过"],
    "tisvc-sjtu-2003": ["需关注", "未找到", "未找到", "未找到", "需关注", "未找到", "需关注", "需关注", "未找到", "需关注", "通过", "需关注", "通过"],
}

_FIXTURE_DIR = ROOT / "fixtures-tisvc"


def _item(text: str, item_id: str) -> dict:
    out = checklist.run_checklist(text, category="technical_service")
    return next(i for i in out["items"] if i["id"] == item_id)


def _review(fname: str) -> dict:
    text = (_FIXTURE_DIR / f"{fname}.txt").read_text(encoding="utf-8")
    out = checklist.run_checklist(text, category="technical_service")
    return {i["id"]: i for i in out["items"]}


@pytest.mark.parametrize("fname", sorted(GOLD_MATRIX))
def test_gold_matrix_locked(fname: str) -> None:
    """金标逐格断言：65 格直锁（老钱签认 2026-10-16，不允许静默重算）。"""
    assert len(ITEMS) == 13, "清单项数漂移：金标矩阵按 13 项锁定"
    assert len(GOLD_MATRIX[fname]) == 13, (
        "金标行长度≠13：zip 会静默截断，必须补齐后再断言")
    row = _review(fname)
    for item_id, expected in zip(ITEMS, GOLD_MATRIX[fname]):
        actual = row[item_id]["status"]
        assert actual == expected, (
            f"金标翻格：{fname} {item_id} 金标={expected} 实判={actual}"
            "——PR 须附 65 格 diff+法务理由，金标修订需老钱签认")


# ---------- 3 陷阱专项 ----------

def test_trap_crossline_confidentiality_guide() -> None:
    """陷阱①：catdoc 跨行拆分「保密义务如下\\n：」——空白制式模板不得洗成
    通过（F-12「字段名不创设义务」同款）；须未找到/需关注+blank_flag 双断言。"""
    item = _review("tisvc-most-2001")["confidentiality"]
    assert item["status"] == "需关注"
    assert item.get("blank_flag") is True


def test_trap_blank_payment_total_not_pass() -> None:
    """陷阱②：空白「技术服务费总额为：____」不得被「服务费」裸词洗成通过。"""
    row = _review("tisvc-most-2001")
    assert row["payment"]["status"] == "未找到"


def test_trap_boilerplate_breach_clause_not_pass() -> None:
    """陷阱③：示范括注「（支付违约金或损失赔偿额的计算方法）」不得被
    「违约金」裸词洗成通过（techdev 两份均为该形态）。"""
    for fname in ("techdev-coop-most-2001", "techdev-most-2001"):
        row = _review(fname)
        assert row["breach"]["status"] == "未找到", fname


# ---------- 对抗样例（老钱文书指定，实填合同保护） ----------

def test_payment_filled_amount_passes() -> None:
    """实填「人民币壹拾万元整」→须通过（金额锚定不误伤实填合同）。"""
    out = checklist.run_checklist(
        "技术服务费总额为：人民币壹拾万元整。验收合格后支付。", category="technical_service")
    item = next(i for i in out["items"] if i["id"] == "payment")
    assert item["status"] == "通过"


def test_breach_filled_percentage_passes() -> None:
    """实填「违约金按总额0.5%/日」→须通过（括注负向不误伤实填）。"""
    out = checklist.run_checklist(
        "违约方应支付违约金，按合同总额0.5%/日。", category="technical_service")
    item = next(i for i in out["items"] if i["id"] == "breach")
    assert item["status"] == "通过"


def test_subject_filled_officer_passes() -> None:
    """实填「法定代表人：张三」→须通过（填值锚不误伤实填）。"""
    out = checklist.run_checklist(
        "委托方：甲公司，法定代表人：张三，住所：北京市海淀区。",
        category="technical_service")
    item = next(i for i in out["items"] if i["id"] == "subject")
    assert item["status"] == "通过"


def test_subject_double_colon_not_pass() -> None:
    r"""肉饼门禁 P2-1 探针固化：连号冒号「法定代表人：：张三」不是填值
    （签认口径 [^\s_＿:：]，冒号非值字符）——有称谓词在场仍须需关注。"""
    item = _item("甲方：甲公司，乙方：乙公司，法定代表人：：张三。", "subject")
    assert item["status"] == "需关注"


@pytest.mark.parametrize("text", [
    "委托人（甲方）：某科技有限责任公司\n法定代表人：（签章）",
    "甲方：甲公司\n法定代表人：（签字）\n住所：（略）",
])
def test_subject_signature_placeholder_not_pass(text: str) -> None:
    """小智娘门禁 P1-1 探针固化：签字栏占位「（签章/签字/略）」不是填值
    （病根残留通道：括号占位洗白主体项）。"""
    item = _item(text, "subject")
    assert item["status"] == "需关注", text


def test_subject_value_with_trailing_stamp_still_passes() -> None:
    """括号只挡首位：「法定代表人：李四（签名）」实填不受影响。"""
    item = _item("委托方：甲公司，法定代表人：李四（签名）。", "subject")
    assert item["status"] == "通过"
