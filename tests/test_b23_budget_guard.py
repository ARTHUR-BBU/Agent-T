# -*- coding: utf-8 -*-
"""B2-3 校准脚本预算闸守卫（用户复核 P1：$5 硬上限进执行路径）。

场景：①最后一笔刚好够→发 ②下一笔超限→不发请求+skipped 计数
③纯函数单价表可审计。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools" / "b23"))

from shadow_calibration import (  # noqa: E402  # 需先注入 sys.path
    PRICE_TABLE, check_budget, load_text, estimate_request_cost_usd, run_calibration)


def test_price_table_covers_providers() -> None:
    """计费表覆盖双供应商且为正数（单价更新须同步改表+重审）。"""
    for p in ("jev", "glm"):
        assert PRICE_TABLE[p] > 0


def test_estimate_cost_monotonic_in_length() -> None:
    """成本上界随文本长度单调增（纯函数可审计）。"""
    assert (estimate_request_cost_usd("jev", "a" * 1000)
            < estimate_request_cost_usd("jev", "a" * 10000))


def _fake_samples(n: int) -> list[tuple[str, str, str]]:
    # 统一用真实 fixture（8426 字符→截断 head+tail），预算按 load_text 实产估算
    return [(f"s{i}", "fixtures-classification/tech-consulting-shgov.txt", "consulting")
            for i in range(n)]


def _unit_cost() -> float:
    text, _cov = load_text("fixtures-classification/tech-consulting-shgov.txt")
    return estimate_request_cost_usd("jev", text)


def test_budget_exact_fit_sends_all() -> None:
    """最后一笔刚好够→全部发出（check_budget 用 ≤）。"""
    calls = []

    def fake_caller(text: str):
        calls.append(text)
        return {"category": "consulting", "confidence": 0.9}, {}

    samples = _fake_samples(3)
    budget = _unit_cost() * 3 + 1e-6  # 3 份刚好够（微余量抵浮点尾差）
    _correct, total, spent, skipped = run_calibration(
        samples, fake_caller, "jev", "h" * 12, budget, lambda _l: None)
    assert total == 3 and skipped == 0, (total, skipped)
    assert len(calls) == 3
    assert spent <= budget + 1e-9


def test_budget_exhausted_blocks_request() -> None:
    """下一笔超限→不发请求（caller 零调用）+skipped 计数。"""
    calls = []

    def fake_caller(text: str):
        calls.append(text)
        return {"category": "consulting", "confidence": 0.9}, {}

    samples = _fake_samples(5)
    budget = _unit_cost() * 2 + 1e-6  # 只够 2 份（微余量抵浮点尾差）
    _correct, total, spent, skipped = run_calibration(
        samples, fake_caller, "jev", "h" * 12, budget, lambda _l: None)
    assert len(calls) == total == 2, (len(calls), total)
    assert skipped == 3
    assert spent <= budget + 1e-9


def test_check_budget_boundary() -> None:
    """闸门边界：等于剩余额度放行，超过一分也拦。"""
    assert check_budget(0.05, 0.05)
    assert not check_budget(0.05, 0.05001)
