"""Adversarial paraphrase fixture: same 7 risks, different Chinese wording."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.services.checklist import run_checklist

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "fixtures" / "procurement_paraphrase.txt"
ORIGINAL = ROOT / "fixtures" / "procurement_sample.txt"

MUST_ATTENTION = {
    "payment": "价款与支付",
    "breach": "违约责任",
    "unfair_terms": "格式条款/明显单方不公平",
    "subject": "主体",
    "subject_matter": "标的",
    "governing_law": "适用法律",
    "signature": "签署与印章",
}


@pytest.fixture(scope="module")
def paraphrase_text() -> str:
    return FIXTURE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def result(paraphrase_text: str):
    return run_checklist(paraphrase_text, "procurement")


@pytest.fixture(scope="module")
def by_id(result):
    return {i["id"]: i for i in result["items"]}


def test_paraphrase_fixture_exists_and_differs():
    assert FIXTURE.exists()
    para = FIXTURE.read_text(encoding="utf-8")
    orig = ORIGINAL.read_text(encoding="utf-8")
    assert para != orig
    # Old brittle keywords should largely be absent (adversarial wording)
    brittle = [
        "合同全款",
        "签收即视为验收",
        "无需提供质保",
        "甲方不得追究乙方违约责任",
        "不得以发票问题拒付",
        "办公主流配置",
        "（盖章）",
    ]
    for kw in brittle:
        assert kw not in para, f"paraphrase still contains brittle keyword: {kw}"
    # Synonym cues present
    assert "一次性付清" in para or "预付全部货款" in para
    assert "收货即合格" in para or "签收视为合格" in para
    assert "免质保" in para or "无质保期" in para
    assert "放弃追究" in para or "免除违约责任" in para
    assert "发票不影响付款" in para
    assert "常规办公配置" in para or "通用办公配置" in para


@pytest.mark.parametrize("item_id,name", list(MUST_ATTENTION.items()))
def test_paraphrase_must_attention(by_id, item_id, name):
    item = by_id[item_id]
    assert item["name"] == name
    assert item["status"] == "需关注", (
        f"{name} ({item_id}) expected 需关注, got {item['status']}: {item.get('note')}"
    )


def test_paraphrase_jurisdiction_passes(by_id):
    item = by_id["jurisdiction"]
    assert item["status"] == "通过", (
        f"管辖与争议 expected 通过, got {item['status']}: {item.get('note')}"
    )


def test_paraphrase_all_seven(by_id):
    flagged = [iid for iid in MUST_ATTENTION if by_id[iid]["status"] == "需关注"]
    assert len(flagged) == 7
