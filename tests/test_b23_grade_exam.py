# -*- coding: utf-8 -*-
"""阅卷器判分单元测试（Codex 勘正 2026-10-22：拒收题 other/None 不得计命中）。"""
from __future__ import annotations

import tools.b23.grade_exam as ge


def test_reject_explicit_class_passes() -> None:
    for pred in ("consulting", "labor_dispatch", "tech_transfer"):
        ok, verdict = ge.judge(pred, "拒收", None, performed=True)
        assert ok, f"{pred} 应计拒收命中"
        assert "显式拒类" in verdict


def test_reject_other_fails() -> None:
    """other=存疑按用户原选择继续，不是拒收——不得计命中（假绿防线）。"""
    ok, verdict = ge.judge("other", "拒收", None, performed=True)
    assert not ok
    assert "other" in verdict


def test_reject_none_fails() -> None:
    """基础设施故障/未判（pred=None）不是「未收=对」——不得计命中。"""
    ok, _ = ge.judge(None, "拒收", None, performed=False)
    assert not ok


def test_reject_supported_class_fails() -> None:
    ok, _ = ge.judge("procurement", "拒收", None, performed=True)
    assert not ok


def test_collect_exact_match_only() -> None:
    ok, _ = ge.judge("lease", "收", "lease", performed=True)
    assert ok
    ok, verdict = ge.judge("nda", "收", "lease", performed=True)
    assert not ok
    assert verdict == "误收" or "误" in verdict
