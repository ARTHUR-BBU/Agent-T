"""Rule hit strings must be returned for quote highlighting."""
from pathlib import Path

from app.services.checklist import run_checklist

ROOT = Path(__file__).resolve().parents[1]


def test_subject_matter_hits_in_quote():
    text = (ROOT / "fixtures" / "procurement_sample.txt").read_text(encoding="utf-8")
    by = {i["id"]: i for i in run_checklist(text, "procurement")["items"]}
    item = by["subject_matter"]
    assert item["status"] == "需关注"
    assert item.get("hits"), "expected rule hits for highlighting"
    assert any(h in item["quote"] for h in item["hits"])


def test_ask_available_flag_on_review():
    # smoke: schema accepts hits list on items
    text = (ROOT / "fixtures" / "procurement_sample.txt").read_text(encoding="utf-8")
    item = run_checklist(text, "procurement")["items"][0]
    assert "hits" in item
