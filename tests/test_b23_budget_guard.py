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
    PRICE_TABLE, check_budget, load_text, estimate_request_cost_usd,
    run_batch_with_preflight, run_calibration)


def test_price_table_covers_providers() -> None:
    """计费表覆盖双供应商：Jev 实测单价（控制台 2026-10-17 ≈$0.041/M，上取整
    0.05 留余量）须为正；GLM 免费档计 0（商用定价后更新）。"""
    assert PRICE_TABLE["jev"] > 0
    assert PRICE_TABLE["glm"] >= 0


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


def test_preflight_blocks_mid_batch_shortfall(tmp_path) -> None:
    """复核 P1：小样本先通过→大样本超预算→后面小样本——预检须在首个
    请求前拦下整批：零调用、零记录、零输出文件、退出码 2。"""
    calls = []

    def fake_caller(text: str):
        calls.append(text)
        return {"category": "consulting", "confidence": 0.9}, {}

    small1 = tmp_path / "small1.txt"
    small1.write_text("乙" * 100, encoding="utf-8")
    big = tmp_path / "big.txt"
    big.write_text("甲" * 30000, encoding="utf-8")
    small2 = tmp_path / "small2.txt"
    small2.write_text("丙" * 100, encoding="utf-8")
    samples = [(n, str(p), "consulting") for n, p in
               (("small-1", small1), ("big", big), ("small-2", small2))]

    # 预算=只够第一份小样本：整批总上界必超（旧实现会发 1 次请求+写 1 条留痕）
    unit_small = estimate_request_cost_usd("jev", "乙" * 100)
    budget = unit_small * 1 + 1e-6
    out_path = tmp_path / "out" / "shadow_test.jsonl"

    code, _c, total, _spent, skipped = run_batch_with_preflight(
        samples, fake_caller, "jev", "h" * 12, budget, out_path)

    assert code == 2
    assert calls == [], f"仍发了 {len(calls)} 个请求——预检未生效"
    assert total == 0
    assert not out_path.exists(), "半截校准文件被写出"
    assert not out_path.parent.exists() or not any(out_path.parent.iterdir()), "输出目录被污染"


def test_budget_stop_whole_batch_on_first_exceed(monkeypatch, tmp_path) -> None:
    """复核 P2：首次超限即停整批——大样本被拦后，后面更小的样本也
    不得发请求（防半截批次污染校准记录）；writer 只落已发样本。"""
    calls = []

    def fake_caller(text: str):
        calls.append(text)
        return {"category": "consulting", "confidence": 0.9}, {}

    # 大样本（住建部 5.2 万字）在前，小样本在后——复现受控场景：
    # 大样本超预算，若实现错误（continue），后面小样本仍会被调用
    big = tmp_path / "big.txt"
    big.write_text("甲" * 30000, encoding="utf-8")
    small = tmp_path / "small.txt"
    small.write_text("乙" * 100, encoding="utf-8")
    samples = [(n, str(p), "consulting") for n, p in
               (("big", big), ("small-1", small), ("small-2", small))]

    # 预算=小样本单份成本×2：够 0 份大样本，但若错误实现会够跑后面小样本
    small_cost = estimate_request_cost_usd("jev", "乙" * 100)
    budget = small_cost * 2 + 1e-6

    lines: list[str] = []
    _c, total, spent, skipped = run_calibration(
        samples, fake_caller, "jev", "h" * 12, budget, lines.append)
    assert calls == [], f"首次超限后仍发了 {len(calls)} 个请求"
    assert total == 0
    assert skipped == 3, skipped
    assert lines == [], "超限后不应有任何留痕落盘"
    assert spent == 0.0
