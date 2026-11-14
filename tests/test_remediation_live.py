"""补写的少量合成合同实测；仅显式 REMEDIATION_LIVE=1 时调用供应商。"""
import os

import pytest

from app.services import llm_remediate


pytestmark = [
    pytest.mark.live_llm,
    pytest.mark.skipif(os.getenv("REMEDIATION_LIVE") != "1", reason="需显式开启真实模型测试"),
]


def test_live_finds_payment_elsewhere():
    result = llm_remediate.remediate_item(
        item={"id": "payment", "name": "付款安排", "note": "合同应写明付款安排"},
        contract_text="采购协议。双方另行约定：货到验收后由买方向卖方支付全部货款。",
        category="procurement")
    assert result["mode"] == "elsewhere", result.get("error")
    assert result["quote_verified"] is True
    assert not result["draft"]["needed"]


def test_live_missing_payment_example_has_no_decision_value():
    result = llm_remediate.remediate_item(
        item={"id": "payment", "name": "付款安排", "note": "合同应写明付款安排"},
        contract_text="采购协议。卖方向买方交付双方另行确认的商品。双方签字后生效。",
        category="procurement")
    assert result["mode"] == "draft", result.get("error")
    assert result["draft"]["clause_draft"]
    assert not llm_remediate._DECISION_VALUE.search(result["draft"]["clause_draft"])


def test_live_segmented_contract_finds_middle():
    text = "本段为交付说明。" * 650 + "货到验收后由买方向卖方支付全部货款。" + "双方应配合交付。" * 650
    result = llm_remediate.remediate_item(
        item={"id": "payment", "name": "付款安排", "note": "合同应写明付款安排"},
        contract_text=text, category="procurement")
    assert result["mode"] == "elsewhere", result.get("error")
    assert result["quote_verified"] is True


def test_live_segmented_contract_produces_draft_after_full_scan():
    text = "交付时双方应核对货物。" * 700 + "双方按约定配合交付。" * 250
    result = llm_remediate.remediate_item(
        item={"id": "payment", "name": "付款安排", "note": "合同应写明付款安排"},
        contract_text=text, category="procurement")
    assert result["mode"] == "draft", result.get("error")
    assert result["draft"]["clause_draft"]
