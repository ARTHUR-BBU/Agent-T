"""Gold-standard: procurement fixture must flag 7× 需关注."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.services.checklist import run_checklist

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "fixtures" / "procurement_sample.txt"

# Acceptance: these MUST be 需关注
MUST_ATTENTION = {
    "payment": "价款与支付",
    "breach": "违约责任",
    "unfair_terms": "格式条款/明显单方不公平",
    "subject": "主体",
    "subject_matter": "标的",
    "governing_law": "适用法律",
    "signature": "签署与印章",
}

# 管辖与争议 can 通过
MAY_PASS = {"jurisdiction": "管辖与争议"}


@pytest.fixture(scope="module")
def procurement_text() -> str:
    return FIXTURE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def result(procurement_text: str):
    return run_checklist(procurement_text, "procurement")


@pytest.fixture(scope="module")
def by_id(result):
    return {i["id"]: i for i in result["items"]}


def test_fixture_exists():
    assert FIXTURE.exists()
    text = FIXTURE.read_text(encoding="utf-8")
    assert "长江有限公司办公设备采购合同" in text
    assert "甲方不得追究乙方违约责任" in text


def test_twelve_items(result):
    assert len(result["items"]) == 12


@pytest.mark.parametrize("item_id,name", list(MUST_ATTENTION.items()))
def test_must_attention(by_id, item_id, name):
    item = by_id[item_id]
    assert item["name"] == name
    assert item["status"] == "需关注", (
        f"{name} ({item_id}) expected 需关注, got {item['status']}: {item.get('note')}"
    )


def test_jurisdiction_can_pass(by_id):
    item = by_id["jurisdiction"]
    assert item["status"] == "通过", (
        f"管辖与争议 expected 通过, got {item['status']}: {item.get('note')}"
    )


def test_all_seven_attention_count(by_id):
    flagged = [iid for iid in MUST_ATTENTION if by_id[iid]["status"] == "需关注"]
    assert len(flagged) == 7


def test_pipeline_on_fixture():
    from app.graph.pipeline import run_review

    raw = FIXTURE.read_bytes()
    out = run_review("procurement_sample.txt", raw, "procurement")
    assert not out.get("error")
    by = {i["id"]: i for i in out["items"]}
    for iid in MUST_ATTENTION:
        assert by[iid]["status"] == "需关注"
    assert by["jurisdiction"]["status"] == "通过"


def test_nda_payment_na():
    nda = (ROOT / "fixtures" / "nda_public_template.txt").read_text(encoding="utf-8")
    out = run_checklist(nda, "nda")
    by = {i["id"]: i for i in out["items"]}
    assert by["payment"]["status"] == "本类不适用"
