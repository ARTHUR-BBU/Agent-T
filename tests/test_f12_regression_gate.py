# -*- coding: utf-8 -*-
"""F-12 施工批1 · 回归门禁自测（外审验收整改卡③）。

门禁只有非零退出码才能拦坏提交——守门员装好后必须用测试烟确认真的会响：
人为制造靶向外翻转，脚本必须退出 1；干净基线必须退出 0。
"""
from __future__ import annotations

import copy
from pathlib import Path

import tools.m65.regression_batch1 as reg


def test_gate_passes_on_clean_baseline(tmp_path: Path, monkeypatch) -> None:
    """干净基线：17 份全绿 + 靶向外零翻转 → 退出码 0。"""
    monkeypatch.setattr(reg, "_baseline", reg._baseline)  # 真基线，无篡改
    rc, report = reg.run_regression(str(tmp_path / "r.json"))
    assert rc == 0, f"干净基线应通过，实际退出码 {rc}"
    assert report["flipped_non_target"] == 0
    assert len(report["rows"]) == reg.EXPECTED_CONTRACTS


def test_gate_blocks_injected_extra_flip(tmp_path: Path, monkeypatch) -> None:
    """人为篡改基线制造一条靶向外翻转（外审 INJECTED_EXTRA_FLIPS 手法）：
    脚本必须非零退出——否则红线只是汇报材料，拦不住坏提交。"""
    real = reg._baseline()
    tampered = copy.deepcopy(real)
    # 找一份 live 判「通过」的 jurisdiction，把基线篡改成「未找到」→
    # live 与基线不符 → 靶向外翻转 → 门禁必须响
    changed = False
    for fname, items in tampered.items():
        if items.get("jurisdiction") == "通过":
            items["jurisdiction"] = "未找到"
            changed = True
            break
    assert changed, "找不到可篡改的 jurisdiction=通过 基线（fixture 结构变化？）"
    monkeypatch.setattr(reg, "_baseline", lambda: tampered)
    rc, report = reg.run_regression(str(tmp_path / "r.json"))
    assert rc == 1, "注入靶向外翻转后门禁未拦截（退出码 0）"
    assert report["flipped_non_target"] >= 1


def test_gate_blocks_missing_baseline_coverage(tmp_path: Path, monkeypatch) -> None:
    """基线覆盖缺文件（新合同没基线）→ 门禁必须响——防悄悄少合同也全绿。"""
    real = reg._baseline()
    tampered = copy.deepcopy(real)
    tampered.pop(next(iter(tampered)))
    monkeypatch.setattr(reg, "_baseline", lambda: tampered)
    rc, _report = reg.run_regression(None)
    assert rc == 1


def test_gate_blocks_deleted_item_key(tmp_path: Path, monkeypatch) -> None:
    """检查项被删/改名（基线有、live 无）→ 门禁必须响（外审 PR #92 销项：
    diffs 只遍历 now 的键会漏掉基线独有键——删除无关项也全绿的盲区）。"""
    real = reg._baseline()
    tampered = copy.deepcopy(real)
    # 删掉某基线记录里一个授权靶向外的键，模拟引擎侧该项消失
    for items in tampered.values():
        if "jurisdiction" in items:
            del items["jurisdiction"]
            break
    orig_run = reg.run_checklist

    def filtered(text, category):
        r = orig_run(text, category)
        r["items"] = [i for i in r["items"] if i["id"] != "jurisdiction"]
        return r

    monkeypatch.setattr(reg, "_baseline", lambda: tampered)
    monkeypatch.setattr(reg, "run_checklist", filtered)
    rc, report = reg.run_regression(None)
    assert rc == 1, "删除检查项后门禁未拦截（键并集盲区）"
    assert report["flipped_non_target"] >= 1


def test_gate_blocks_contract_count_drift(monkeypatch) -> None:
    """语料盘点数漂移（少一份合同）→ 门禁必须响——盘点变化必须显式
    更新 EXPECTED_CONTRACTS，防悄悄少了合同也全绿。"""
    import json

    real_manifest = json.loads(
        (reg.ROOT / "fixtures-real" / "manifest.json").read_text(encoding="utf-8"))
    assert len(real_manifest) == 17
    drifted = dict(list(real_manifest.items())[:-1])
    monkeypatch.setattr(reg, "_load_manifest", lambda: drifted)
    rc, _ = reg.run_regression(None)
    assert rc == 1
