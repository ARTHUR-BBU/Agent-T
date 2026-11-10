"""Gold-standard heuristics for fixtures/procurement_sample.txt."""
from __future__ import annotations

from pathlib import Path

from app.services.checklist import run_checklist

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "fixtures" / "procurement_sample.txt"

# Must be 需关注 on the gold fixture
MUST_ATTENTION = {
    "payment",
    "breach",
    "unfair_terms",
    "subject",
    "subject_matter",
    "governing_law",
    "signature",
}


def test_procurement_gold_attention_items():
    text = FIXTURE.read_text(encoding="utf-8")
    result = run_checklist(text, category="procurement")
    by_id = {it["id"]: it for it in result["items"]}

    missing = MUST_ATTENTION - set(by_id)
    assert not missing, f"checklist missing items: {missing}"

    for item_id in MUST_ATTENTION:
        status = by_id[item_id]["status"]
        assert status == "需关注", f"{item_id} expected 需关注, got {status}: {by_id[item_id]}"


def test_jurisdiction_passes():
    text = FIXTURE.read_text(encoding="utf-8")
    result = run_checklist(text, category="procurement")
    by_id = {it["id"]: it for it in result["items"]}
    assert by_id["jurisdiction"]["status"] == "通过"


def test_term_passes():
    text = FIXTURE.read_text(encoding="utf-8")
    result = run_checklist(text, category="procurement")
    by_id = {it["id"]: it for it in result["items"]}
    assert by_id["term"]["status"] == "通过"
