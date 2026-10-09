# -*- coding: utf-8 -*-
"""F-12/P2 · 回归门禁自测（外审 PR #94 修复卡④ + 历轮销项固化）。

门禁只有非零退出码才能拦坏提交——守门员装好后必须用测试烟确认真的会响：
注入 payment/breach/signature 任一变化必须退出 1；授权四翻转缺失必须退出 1；
干净（快照+四翻转）必须退出 0。
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import tools.m65.regression_batch1 as reg


def _p2_live() -> dict[str, dict[str, str]]:
    """P2 终态 live（快照+四份授权 term 翻转）——守门员注入的底版。"""
    snap = reg._load_snapshot()
    live = {f: dict(c["status_map"]) for f, c in snap["contracts"].items()}
    for fname, item_id, _was, now in reg.AUTHORIZED_FLIPS:
        live[fname][item_id] = now
    return live


def test_gate_passes_on_p2_terminal_state(tmp_path: Path) -> None:
    """P2 终态（快照+四份授权 term 翻转在场）→ 退出码 0。"""
    rc, report = reg.run_regression(None, live_override=_p2_live())
    assert rc == 0, f"P2 终态应通过，实际退出码 {rc}"
    assert report["flipped_non_target"] == 0
    assert len(report["rows"]) == reg.EXPECTED_CONTRACTS


def _inject(item_id: str) -> dict[str, dict[str, str]]:
    """在 P2 终态上注入一条非授权变化：首份合同的目标项翻成任意不同状态。"""
    live = _p2_live()
    fname = next(iter(live))
    old = live[fname][item_id]
    live[fname][item_id] = "需关注" if old != "需关注" else "未找到"
    assert live[fname][item_id] != old, "注入值不得与快照原值相同（否则无 diff）"
    return live


def test_gate_blocks_payment_change() -> None:
    """注入 payment 状态变化 → 必须退出 1（修复卡④：粗口径放行已废除）。"""
    rc, _ = reg.run_regression(None, live_override=_inject("payment"))
    assert rc == 1


def test_gate_blocks_breach_change() -> None:
    """注入 breach 状态变化 → 必须退出 1。"""
    rc, _ = reg.run_regression(None, live_override=_inject("breach"))
    assert rc == 1


def test_gate_blocks_signature_change() -> None:
    """注入 signature 状态变化 → 必须退出 1。"""
    rc, _ = reg.run_regression(None, live_override=_inject("signature"))
    assert rc == 1


def test_gate_blocks_reverted_authorized_flip() -> None:
    """授权翻转被回退（term 改回「通过」）→ 正向断言必须拦——
    只拦非法翻转的话，零 diff 的回归回退会溜过门禁（外审 P2 #94 销项③）。"""
    live = _p2_live()
    fname = next(f for f, i, _w, _n in reg.AUTHORIZED_FLIPS)
    live[fname]["term"] = "通过"
    rc, _ = reg.run_regression(None, live_override=live)
    assert rc == 1


def test_gate_blocks_missing_baseline_coverage(tmp_path: Path, monkeypatch) -> None:
    """基线覆盖缺文件（新合同没基线）→ 门禁必须响——防悄悄少合同也全绿。"""
    real = reg._baseline()
    tampered = copy.deepcopy(real)
    tampered.pop(next(iter(tampered)))
    monkeypatch.setattr(reg, "_baseline", lambda: tampered)
    rc, _ = reg.run_regression(None)
    assert rc == 1


def test_gate_blocks_contract_count_drift(monkeypatch) -> None:
    """语料盘点数漂移（少一份合同）→ 门禁必须响——盘点变化必须显式
    更新 EXPECTED_CONTRACTS，防悄悄少了合同也全绿。"""
    real_manifest = json.loads(
        (reg.ROOT / "fixtures-real" / "manifest.json").read_text(encoding="utf-8"))
    assert len(real_manifest) == 17
    drifted = dict(list(real_manifest.items())[:-1])
    monkeypatch.setattr(reg, "_load_manifest", lambda: drifted)
    rc, _ = reg.run_regression(None)
    assert rc == 1


def test_snapshot_source_is_pinned() -> None:
    """快照来源提交固定为 d575a10（施工前状态）——快照被换包门禁即不可信。"""
    snap = reg._load_snapshot()
    assert snap["source_commit"] == "d575a10"
    assert len(snap["contracts"]) == reg.EXPECTED_CONTRACTS
