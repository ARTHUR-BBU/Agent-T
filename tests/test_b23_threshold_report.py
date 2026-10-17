# -*- coding: utf-8 -*-
"""B2-3 阈值报告分布对账守卫（复核 P1 三犯防线）。

报告 §1 分布表的档位数字必须能从入库校准 jsonl 的置信度字段自动复算——
手写分布漂移立即炸红。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "docs" / "b23" / "threshold-analysis-jev-r1.md"
CAL_JSONL = ROOT / "docs" / "b23" / "shadow_jev_calibration_20261017_173606.jsonl"


def test_report_distribution_recomputable_from_jsonl() -> None:
    rows = [json.loads(line) for line in CAL_JSONL.read_text(encoding="utf-8").splitlines()]
    confs = [r["verdict"]["confidence"] for r in rows]
    truth = {
        "=1.00": sum(1 for c in confs if c == 1.0),
        "0.97-0.99": sum(1 for c in confs if 0.97 <= c < 1.0),
        "0.91-0.92": sum(1 for c in confs if 0.91 <= c < 0.97),
        "<0.90": sum(1 for c in confs if c < 0.90),
    }
    assert sum(truth.values()) == 22

    report = REPORT.read_text(encoding="utf-8")
    # 报告表行形如「| =1.00 | 18 | ...」（数字容忍 **加粗**）——与 jsonl 复算一致
    for tier, n in truth.items():
        m = re.search(rf"\|\s*{re.escape(tier)}\s*\|\s*(?:\*\*)?(\d+)(?:\*\*)?\s*\|", report)
        assert m, f"报告缺档位行：{tier}"
        assert int(m.group(1)) == n, (
            f"分布漂移：{tier} 报告写 {m.group(1)}，jsonl 复算={n}——"
            "报告分布必须由 jsonl 自动复算，禁手写")

    # 「≥0.90 全覆盖」声明与真值一致（22/22）
    # 若存在 <0.90 样本，报告不得声称 22/22 全落 ≥0.90
    all_ge = all(c >= 0.90 for c in confs)
    if all_ge:
        assert "22/22 全部 ≥0.90" in report or "22/22 全落" in report
    else:
        assert "22/22 全部 ≥0.90" not in report, "留痕有 <0.90 样本，报告不得声称全覆盖"
