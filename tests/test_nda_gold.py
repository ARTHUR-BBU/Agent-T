"""M3 NDA gold: 12-item checklist, payment always N/A, red-line 需关注."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services.checklist import run_checklist
from app.services.blind_spot import annotate_rule_items, run_blind_spot_pass

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures"
PUBLIC = FIXTURES / "nda_public_template.txt"
GOLD_RISKS = FIXTURES / "nda_gold_risks.txt"
ADVERSARIAL = FIXTURES / "nda_adversarial.txt"

EXPECTED_IDS = [
    "subject",
    "confidentiality_scope",
    "payment",
    "agreement_term",
    "confidentiality_duration",
    "breach",
    "unfair_terms",
    "ip",
    "jurisdiction",
    "termination",
    "governing_law",
    "signature",
]

EXPECTED_NAMES = {
    "subject": "主体（谁跟谁签）",
    "confidentiality_scope": "保密范围与义务（什么算秘密、要怎么守）",
    "payment": "价款与支付",
    "agreement_term": "协议期限（这份协议管多久）",
    "confidentiality_duration": "保密存续期（秘密要守到哪一天——和协议期限分开看）",
    "breach": "违约责任（违约怎么赔）",
    "unfair_terms": "格式条款 / 单方不公平（有没有一边倒）",
    "ip": "知识产权归属（成果/秘密相关权利归谁）",
    "jurisdiction": "管辖与争议（吵起来找谁管）",
    "termination": "解除与终止（怎么收场）",
    "governing_law": "适用法律（按哪国/哪地法律）",
    "signature": "签署与印章（谁有权签字、章齐不齐）",
}

# Red lines that must NOT be 通过 on risk fixtures
RED_LINE_IDS = {
    "governing_law",
    "confidentiality_duration",
    "ip",
    "unfair_terms",
    "signature",
}

STATUS_NA = "本类不适用"
STATUS_ATTENTION = "需关注"
STATUS_PASS = "通过"


def _by_id(text: str) -> dict:
    result = run_checklist(text, "nda")
    return result, {i["id"]: i for i in result["items"]}


@pytest.fixture(scope="module")
def public_text() -> str:
    return PUBLIC.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def gold_text() -> str:
    return GOLD_RISKS.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def adv_text() -> str:
    return ADVERSARIAL.read_text(encoding="utf-8")


def test_nda_fixtures_exist_and_differ(public_text, gold_text, adv_text):
    assert PUBLIC.exists() and GOLD_RISKS.exists() and ADVERSARIAL.exists()
    assert public_text != gold_text != adv_text
    assert gold_text != adv_text


def test_nda_checklist_twelve_items_and_names(public_text):
    result, by_id = _by_id(public_text)
    assert len(result["items"]) == 12
    assert [i["id"] for i in result["items"]] == EXPECTED_IDS
    for iid, name in EXPECTED_NAMES.items():
        assert by_id[iid]["name"] == name


@pytest.mark.parametrize(
    "path",
    [PUBLIC, GOLD_RISKS, ADVERSARIAL],
    ids=lambda p: p.name,
)
def test_nda_payment_always_na(path: Path):
    text = path.read_text(encoding="utf-8")
    _, by_id = _by_id(text)
    item = by_id["payment"]
    assert item["status"] == STATUS_NA
    assert item["status"] != "未找到"
    note = item.get("note") or ""
    assert item["status"] != "未找到"
    assert "本类无对价" in note or "本类不适用" in note


def test_public_template_passes_where_appropriate(public_text):
    _, by_id = _by_id(public_text)
    assert by_id["payment"]["status"] == STATUS_NA
    should_pass = [
        "subject",
        "confidentiality_scope",
        "agreement_term",
        "confidentiality_duration",
        "breach",
        "unfair_terms",
        "ip",
        "jurisdiction",
        "termination",
        "governing_law",
        "signature",
    ]
    for iid in should_pass:
        assert by_id[iid]["status"] == STATUS_PASS, (
            f"public {iid} expected 通过, got {by_id[iid]['status']}: {by_id[iid].get('note')}"
        )


@pytest.mark.parametrize(
    "path",
    [GOLD_RISKS, ADVERSARIAL],
    ids=lambda p: p.name,
)
def test_red_lines_need_attention_on_risk_fixtures(path: Path):
    text = path.read_text(encoding="utf-8")
    result, by_id = _by_id(text)
    assert len(result["items"]) == 12
    for iid in RED_LINE_IDS:
        item = by_id[iid]
        assert item["status"] == STATUS_ATTENTION, (
            f"{path.name}: {item['name']} ({iid}) expected 需关注, "
            f"got {item['status']}: {item.get('note')}"
        )
    # payment still N/A, never 未找到
    assert by_id["payment"]["status"] == STATUS_NA


def test_gold_risks_jurisdiction_without_governing_law(gold_text):
    _, by_id = _by_id(gold_text)
    assert by_id["jurisdiction"]["status"] == STATUS_PASS
    assert by_id["governing_law"]["status"] == STATUS_ATTENTION


def test_adversarial_avoids_gold_risk_brittle_phrases(gold_text, adv_text):
    brittle = [
        "永久保密",
        "甲方不得追究乙方违约责任",
        "盖章确认",
        "（盖章）",
        "接收方无偿取得",
        "最终解释权",
    ]
    for kw in brittle:
        assert kw in gold_text, f"gold_risks should contain {kw}"
        assert kw not in adv_text, f"adversarial still contains gold keyword: {kw}"


def test_blind_spot_does_not_overwrite_nda_rule_tags(gold_text, monkeypatch):
    monkeypatch.setenv("BLIND_SPOT_ENABLED", "true")
    result, by_id = _by_id(gold_text)
    items = annotate_rule_items(result["items"])
    att = next(i for i in items if i["id"] in RED_LINE_IDS and i["status"] == STATUS_ATTENTION)
    assert att.get("tag_source") == "rule"
    snippet = (att.get("quote") or gold_text[:40]).strip()
    if snippet not in gold_text:
        snippet = gold_text[20:50]

    def chat(_system, _user):
        return json.dumps(
            [
                {
                    "item_id": att["id"],
                    "name": att["name"],
                    "note": "试图覆盖规则标签",
                    "quote": snippet if snippet in gold_text else gold_text[10:40],
                }
            ],
            ensure_ascii=False,
        )

    out = run_blind_spot_pass(text=gold_text, items=items, chat_fn=chat)
    assert all(c["id"] != att["id"] for c in out["blind_candidates"])
    still = next(i for i in items if i["id"] == att["id"])
    assert still["status"] == STATUS_ATTENTION
    assert still.get("tag_source") == "rule"


def test_pipeline_nda_gold_risks():
    from app.graph.pipeline import run_review

    raw = GOLD_RISKS.read_bytes()
    out = run_review("nda_gold_risks.txt", raw, "nda")
    assert not out.get("error")
    by = {i["id"]: i for i in out["items"]}
    assert len(out["items"]) == 12
    assert by["payment"]["status"] == STATUS_NA
    for iid in RED_LINE_IDS:
        assert by[iid]["status"] == STATUS_ATTENTION
        assert by[iid].get("tag_source") == "rule"
