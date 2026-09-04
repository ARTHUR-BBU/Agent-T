"""Adversarial fixtures: same 7 risks, fresh wording beyond gold + paraphrase."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.services.checklist import run_checklist

ROOT = Path(__file__).resolve().parents[1]
FIXTURES_DIR = ROOT / "fixtures"

GOLD = FIXTURES_DIR / "procurement_sample.txt"
PARAPHRASE = FIXTURES_DIR / "procurement_paraphrase.txt"
ADVERSARIAL = [
    FIXTURES_DIR / "procurement_adversarial_2.txt",
    FIXTURES_DIR / "procurement_adversarial_3.txt",
]

MUST_ATTENTION = {
    "payment": "价款与支付",
    "breach": "违约责任",
    "unfair_terms": "格式条款/明显单方不公平",
    "subject": "主体",
    "subject_matter": "标的",
    "governing_law": "适用法律",
    "signature": "签署与印章",
}

# Brittle phrases from gold / paraphrase that adversarial fixtures must avoid
BRITTLE_FROM_PRIOR = [
    "合同全款",
    "签收即视为验收",
    "无需提供质保",
    "甲方不得追究乙方违约责任",
    "不得以发票问题拒付",
    "办公主流配置",
    "（盖章）",
    "一次性付清",
    "预付全部货款",
    "签约即付",
    "收货即合格",
    "签收视为合格",
    "免质保",
    "无质保期",
    "放弃追究",
    "免除违约责任",
    "发票不影响付款",
    "常规办公配置",
    "通用办公配置",
    "盖章确认",
]


@pytest.fixture(scope="module", params=ADVERSARIAL, ids=[p.name for p in ADVERSARIAL])
def adv_path(request) -> Path:
    return request.param


@pytest.fixture(scope="module")
def adv_text(adv_path: Path) -> str:
    return adv_path.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def adv_result(adv_text: str):
    return run_checklist(adv_text, "procurement")


@pytest.fixture(scope="module")
def adv_by_id(adv_result):
    return {i["id"]: i for i in adv_result["items"]}


def test_adversarial_files_exist_and_differ():
    assert GOLD.exists() and PARAPHRASE.exists()
    gold = GOLD.read_text(encoding="utf-8")
    para = PARAPHRASE.read_text(encoding="utf-8")
    for path in ADVERSARIAL:
        assert path.exists(), f"missing {path.name}"
        text = path.read_text(encoding="utf-8")
        assert text != gold
        assert text != para
        for other in ADVERSARIAL:
            if other != path:
                assert text != other.read_text(encoding="utf-8")


def test_adversarial_avoids_prior_brittle_keywords(adv_text: str, adv_path: Path):
    for kw in BRITTLE_FROM_PRIOR:
        assert kw not in adv_text, f"{adv_path.name} still contains prior keyword: {kw}"


@pytest.mark.parametrize("item_id,name", list(MUST_ATTENTION.items()))
def test_adversarial_must_attention(adv_by_id, item_id, name):
    item = adv_by_id[item_id]
    assert item["name"] == name
    assert item["status"] == "需关注", (
        f"{name} ({item_id}) expected 需关注, got {item['status']}: {item.get('note')}"
    )


def test_adversarial_jurisdiction_passes(adv_by_id):
    item = adv_by_id["jurisdiction"]
    assert item["status"] == "通过", (
        f"管辖与争议 expected 通过, got {item['status']}: {item.get('note')}"
    )


def test_adversarial_all_seven(adv_by_id):
    flagged = [iid for iid in MUST_ATTENTION if adv_by_id[iid]["status"] == "需关注"]
    assert len(flagged) == 7


@pytest.mark.parametrize(
    "path",
    [GOLD, PARAPHRASE, *ADVERSARIAL],
    ids=lambda p: p.name,
)
def test_all_procurement_fixtures_seven_attention(path: Path):
    """Regression: gold + paraphrase + adversarial all hit the same 7 risks."""
    text = path.read_text(encoding="utf-8")
    result = run_checklist(text, "procurement")
    by_id = {i["id"]: i for i in result["items"]}
    for iid, name in MUST_ATTENTION.items():
        assert by_id[iid]["status"] == "需关注", (
            f"{path.name}: {name} ({iid}) expected 需关注, got {by_id[iid]['status']}"
        )
    assert by_id["jurisdiction"]["status"] == "通过"
