# -*- coding: utf-8 -*-
"""F-1/F-2 第一批修复 · 金标对抗断言（原型 60 条正式迁移，CI 直接执行）。

数据源：tools/m65/f12_regex_prototype.py 的 _CASES（单一权威）——
施工时生产引擎实现必须让本测试持续全绿；任何词表改动先改原型表再迁移。
"""
from __future__ import annotations

import importlib.util
import re
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


def _internal_caller_names(cases: list) -> list[str]:
    """检测器：找出绕开公开入口、直接调用内部零件的案例名。"""
    internal_parts = (proto._TERM_PROC, proto._PROC_BASELINE)
    offenders = []
    for name, _t, fn, _w in cases:
        owner = getattr(fn, "__self__", None)
        if owner in internal_parts or fn in internal_parts:
            offenders.append(name)
    return offenders


def test_proc_term_cases_use_public_entry() -> None:
    """结构断言（外审 v1.9.1 阻断1）：采购期限案例必须统一调用公开判定入口
    term_proc_pass（独立成表、整函数身份比较——绑定的 .search 方法每次访问
    生成新对象，`is` 抓不到违规，只有检查 __self__ 主人或整函数才可靠）。"""
    offenders = _internal_caller_names(proto._PROC_TERM_CASES)
    assert not offenders, f"以下案例绕开公开入口: {offenders}"
    assert all(fn is proto.term_proc_pass for _, _t, fn, _w in proto._PROC_TERM_CASES)


def test_internal_call_detector_actually_fires() -> None:
    """烟雾报警器测试（元测试反证）：故意注入违规案例，检测器必须点名它——
    守门员装好后必须用测试烟确认真的会响。"""
    bad_case = ("故意违规", "任意文本", proto._TERM_PROC.search, False)
    assert _internal_caller_names([bad_case]) == ["故意违规"]


def test_guardrail_participates() -> None:
    """护栏参与证明：候选入口确实命中、票头核对确实拦下（防候选未命中的假绿）。"""
    for text in ("整改期限为5天内完成验收",
                 "索赔期限为10日内完成交付索赔材料",
                 "举证期限为7日内完成送货证明提交"):
        assert re.search(proto._PROC_BASELINE, text), "候选入口未命中（护栏空转）"
        assert not proto.term_proc_pass(text), "票头核对未拦截"


def test_matcher_contract() -> None:
    """生产匹配器契约预演：一次匹配多处复用，命中对象**五字段**齐备、类型可区分。"""
    hit_wl = proto.match_procurement_term("合同期限自2026年1月1日起至2028年12月31日止")
    assert hit_wl["matched"] and hit_wl["hit_type"] == "whitelist"
    assert hit_wl["evidence"] and hit_wl["start"] is not None and hit_wl["end"] is not None
    hit_num = proto.match_procurement_term("甲方应在30日内完成交付")
    assert hit_num["matched"] and hit_num["hit_type"] == "unlabeled_numeric"
    miss = proto.match_procurement_term("付款期限为30日内")
    assert miss["matched"] is False and miss["hit_type"] == "none"


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


def test_all_cases_green_via_production_engine() -> None:
    """施工收口（设计稿 v1.9.2 十步之⑥）：60 条场景全部切到生产引擎入口重放。
    term→生产 procurement_term 匹配器；F-2→生产 hit_alternatives 豁免门；
    payment/NDA期限/signature→生产加载配置的 pass 规则。原型绿≠生产绿，
    本测试保证原型表（单一权威）与生产实现判定永远一致。"""
    import app.services.checklist as checklist

    proc = checklist.load_checklist("procurement")
    nda = checklist.load_checklist("nda")

    def _rules(cfg: dict, iid: str, phase: str) -> list:
        for it in cfg["items"]:
            if it.get("id") == iid:
                return list(((it.get("rules") or {}).get(phase)) or [])
        raise AssertionError(f"配置缺检查项 {iid}")

    pay_pass = _rules(proc, "payment", "pass")
    nda_term_pass = _rules(nda, "agreement_term", "pass")
    sig_pass = _rules(proc, "signature", "pass")
    breach_na = [r for r in _rules(nda, "breach", "need_attention")
                 if "hit_alternatives" in r]
    assert len(breach_na) == 1, "breach 豁免门规则应恰好一条"

    failed: list[str] = []
    for name, text, fn, want in proto._CASES:
        if fn is proto.term_proc_pass:
            got = bool(checklist._match_procurement_term(text).matched)
        elif fn is proto.f2_verdicts:
            expect_fire = any(v == "触发需关注" for _, v in want)
            hit = checklist._first_unprotected_hit_alt(text, breach_na[0])
            if bool(hit) != expect_fire:
                failed.append(f"{name}: 生产豁免门 hit={hit!r} 期望触发={expect_fire}")
            continue
        elif fn == proto.PAYMENT_PASS.search:
            got = any(checklist._rule_matches(text, r) for r in pay_pass)
        elif fn == proto.TERM_PASS_NDA.search:
            got = any(checklist._rule_matches(text, r) for r in nda_term_pass)
        elif fn == proto.SIGNATURE_PASS.search:
            got = any(checklist._rule_matches(text, r) for r in sig_pass)
        else:
            failed.append(f"{name}: 未识别的案例入口 {fn!r}")
            continue
        if bool(got) != bool(want):
            failed.append(f"{name}: 生产判定={got} 期望={want}")
    assert not failed, "生产入口重放失败：" + "；".join(failed)


def test_unknown_matcher_fails_closed_at_load(tmp_path) -> None:
    """施工十步之③：未知 matcher 必须在配置加载阶段明确报错（fail-closed），
    错误信息含配置文件/检查项/matcher 名；禁止静默不匹配。"""
    import pytest

    import app.services.checklist as checklist

    bad_cfg = {
        "label": "坏配置",
        "items": [
            {"id": "term", "name": "期限",
             "rules": {"pass": [{"matcher": "no_such_matcher"}]}},
        ],
    }
    with pytest.raises(ValueError) as ei:
        checklist._validate_matchers(tmp_path / "checklist_bad.yaml", bad_cfg)
    msg = str(ei.value)
    assert "no_such_matcher" in msg and "term" in msg and "checklist_bad.yaml" in msg
    # 三元组缺字段同责 fail-closed
    bad_alt = {
        "items": [
            {"id": "breach", "name": "违约责任",
             "rules": {"need_attention": [{"hit_alternatives": [{"pattern": "免除违约"}]}]}},
        ],
    }
    with pytest.raises(ValueError) as ei2:
        checklist._validate_matchers(tmp_path / "checklist_bad2.yaml", bad_alt)
    assert "breach" in str(ei2.value) and "hit_alternatives" in str(ei2.value)
