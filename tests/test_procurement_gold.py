"""Gold-standard heuristics for fixtures/procurement_sample.txt."""
from __future__ import annotations

from pathlib import Path

from app.services.checklist import run_checklist

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "fixtures" / "procurement_sample.txt"

# Must be 需关注 on the gold fixture
MUST_ATTENTION = {
    "价款与支付",
    "违约责任",
    "格式条款/明显单方不公平",
    "主体",
    "标的",
    "适用法律",
    "签署与印章",
}


def test_procurement_gold_attention_items():
    text = FIXTURE.read_text(encoding="utf-8")
    result = run_checklist(text, category="procurement")
    by_name = {it["name"]: it for it in result["items"]}

    missing = MUST_ATTENTION - set(by_name)
    assert not missing, f"checklist missing items: {missing}"

    for name in MUST_ATTENTION:
        status = by_name[name]["status"]
        assert status == "需关注", f"{name} expected 需关注, got {status}: {by_name[name]}"


def test_jurisdiction_passes():
    text = FIXTURE.read_text(encoding="utf-8")
    result = run_checklist(text, category="procurement")
    by_name = {it["name"]: it for it in result["items"]}
    assert by_name["管辖与争议"]["status"] == "通过"


def test_term_passes():
    text = FIXTURE.read_text(encoding="utf-8")
    result = run_checklist(text, category="procurement")
    by_name = {it["name"]: it for it in result["items"]}
    assert by_name["期限"]["status"] == "通过"
