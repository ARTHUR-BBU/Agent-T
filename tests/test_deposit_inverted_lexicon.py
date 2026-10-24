# -*- coding: utf-8 -*-
"""定金倒序词表测试（B7-0，老钱裁定 2026-10-24：六件 fixture 全带判别性断言）。

裁定要点：倒序分支与正序同组共用 unless（豁免方向无关，禁止单开无豁免倒序
规则）；动词集=没收/不予退还/不予返还/无权要求返还（正倒序镜像）；「退还定金」
中性清算不入词表；0-20 字间隔+句界阻断（。；换行）跨句不命中。
删除任一分支本文件对应用例必须炸（判别性锁）。
"""
from __future__ import annotations

from app.services.checklist import run_checklist


def _unfair(text: str) -> dict:
    items = run_checklist(text, "procurement")["items"]
    return next(i for i in items if i["id"] == "unfair_terms")


def test_inverted_with_symmetric_penalty_exempted() -> None:
    """① 对称罚则倒序并存 → 豁免，不得判需关注（民法典587 双向对等）。"""
    it = _unfair("乙方中途解约的，甲方有权没收定金；甲方违约的，应双倍返还定金。")
    assert it["status"] != "需关注", "对称定金罚则（倒序命中+双倍返还并存）不得直捕"


def test_inverted_guilty_fees_rebate_not_exempted() -> None:
    """② 货款双倍返还反例（审计 P1-2 反例的倒序版）→ 仍需关注。

    豁免不得被无关「双倍返还货款」类句触发——返还对象不是定金。"""
    it = _unfair("甲方有权没收定金。乙方逾期交货的，已收货款双倍返还乙方。")
    assert it["status"] == "需关注", "单方没收定金+货款双倍返还不构成对称豁免"


def test_neutral_refund_not_matched() -> None:
    """③ 中性退还（正常清算安排）→ 不得命中。"""
    it = _unfair("合同解除后 5 日内，甲方应退还定金。其余条款从约定。")
    assert it["status"] != "需关注", "「退还定金」是中性清算表述，不入罚则词表"


def test_object_not_deposit_not_matched() -> None:
    """④ 宾语非定金（没收履约保证金）→ 不得命中（0-20 字窗内无「定金」）。"""
    it = _unfair("乙方违约的，没收履约保证金。本合同一式两份。")
    assert it["status"] != "需关注", "没收对象是保证金不是定金，不得直捕"


def test_sentence_boundary_blocks_cross_clause() -> None:
    """⑤ 句界阻断（没收的宾语跨句指向不可控）→ 不得命中。"""
    it = _unfair("甲方有权解除合同并没收乙方预付财产。定金于签约日支付。")
    assert it["status"] != "需关注", "跨句后「没收」宾语非定金，句界阻断不得命中"


def test_inverted_solo_confiscation_flagged_with_hit() -> None:
    """⑥ 正例判别性锁：单方倒序没收独立成文 → 需关注且 hits 含「定金」。

    判别性：文本只有倒序形态（无正序「定金…没收」），删掉倒序分支本用例必炸。"""
    it = _unfair("甲方有权解除合同并没收定金，已付定金作为违约赔偿。")
    assert it["status"] == "需关注", "单方倒序没收定金必须直捕（pc11 同构）"
    joined = "".join(it.get("hits") or []) + (it.get("quote") or "")
    assert "定金" in joined, "命中证据必须含「定金」（授权翻转面 hits 口径）"


def test_forward_bu_yu_fan_huan_now_covered() -> None:
    """裁定附带销项：正序补「不予返还」（民法典587 法定用语，原词表漏）。"""
    it = _unfair("乙方违约的，已付定金不予返还。本合同自签字盖章起生效。")
    assert it["status"] == "需关注", "「定金不予返还」单方罚则须直捕"
