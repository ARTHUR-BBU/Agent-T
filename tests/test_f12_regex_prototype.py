# -*- coding: utf-8 -*-
"""F-1/F-2 第一批修复 · 金标对抗断言（原型 60 条正式迁移，CI 直接执行）。

数据源：tools/m65/f12_regex_prototype.py 的 _CASES（单一权威）——
施工时生产引擎实现必须让本测试持续全绿；任何词表改动先改原型表再迁移。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_PROTO = Path(__file__).resolve().parents[1] / "tools" / "m65" / "f12_regex_prototype.py"
_spec = importlib.util.spec_from_file_location("f12_regex_prototype", _PROTO)
assert _spec is not None and _spec.loader is not None
proto = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(proto)


def test_prototype_cases_all_green() -> None:
    """原型全部样例（F-2 逐 occurrence 表 + payment/term/signature 命中布尔）。"""
    failed: list[str] = []
    for name, text, fn, want in proto._CASES:
        got = fn(text) if isinstance(want, list) else bool(fn(text))
        if got != want:
            failed.append(f"{name}: got={got!r} want={want!r}")
    assert not failed, "原型样例失败：" + "；".join(failed)


def test_date_branch_covers_full_range() -> None:
    """日期分支必须覆盖完整日期区间（外审 #91：不能只断言 bool）。"""
    proto._date_span_check()


def test_stress_repeat_hits() -> None:
    """300 处重复命中：窗口化搜索下判定正确（功能性下限；时间上限断言留生产验收）。"""
    proto._stress_repeat_hits()


def test_hit_start_derived_from_authority() -> None:
    """禁行起点必须从单一权威表派生（外审 v1.6 阻断2：不许手写第二份词表）。"""
    derived = r"(?:" + "|".join(dict.fromkeys(start for _, start, _ in proto._HIT_ALTS)) + r")"
    assert proto.HIT_START == derived
    for _, start, _ in proto._HIT_ALTS:
        assert start in proto.HIT_START, f"起点 token 缺失: {start}"


def test_hit_neg_flags_wired() -> None:
    """neg_sensitive 三元组字段必须控制行为（外审 #91 黄项）：逐 alt 编译+旗标接线。"""
    assert proto._HIT_NEG_FLAGS == [neg for _, _, neg in proto._HIT_ALTS]
    assert any(proto._HIT_NEG_FLAGS) and not all(proto._HIT_NEG_FLAGS)
    # 否定敏感命中（免除族）：前置否定 → 不构成命中
    assert proto.f2_verdicts("该约定不会免除其违约责任") == []
    # 非否定敏感命中（不承担违约族）：否定上下文检查跳过，命中照常记录
    # （本句无免责事由 → 豁免链不成立 → 触发需关注，证明否定检查确实被跳过：
    #   若误做否定检查，「任何一方不」前缀会被误判否定而吞掉命中）
    assert proto.f2_verdicts("任何一方不承担违约责任")[0][1] == "触发需关注"


def test_double_negative_not_protected() -> None:
    """双重否定（不得不放弃）不构成保护——专防保护面再次过宽。"""
    t1 = "乙方不得不放弃追究"
    t2 = "乙方不放弃追究"
    t3 = "该约定不会免除其违约责任"
    assert proto._negated(t1, t1.index("放弃")) is False
    assert proto._negated(t2, t2.index("放弃")) is True
    assert proto._negated(t3, t3.index("免除")) is True
