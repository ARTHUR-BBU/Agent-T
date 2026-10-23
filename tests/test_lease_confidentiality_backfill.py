# -*- coding: utf-8 -*-
"""租赁保密四态回填测试（老钱裁定残-1 + 设计稿 v0.1，spec-lease-confidentiality-backfill.md）。

门禁=「翻转白名单」口径：15 份回放卷仅第 1 卷新增「通过」判定，其余 14 份判
「本类不适用」；33 份语料既有判定零翻转（对照改码前冻结基线
tests/data/lease-conf-backfill-baseline.json）。signal_gate 为引擎通用机制：
未配置=原行为不变；已配置但词族坏=load/启动必报错（fail-closed）。
"""
from __future__ import annotations

import copy
import json
import pathlib

import pytest

from app.services.checklist import (
    _signal_gate_problems,
    load_checklist,
    run_checklist,
)

REPO = pathlib.Path(__file__).resolve().parents[1]
REPLAY = REPO / "fixtures" / "replay"
BASELINE_PATH = REPO / "tests" / "data" / "lease-conf-backfill-baseline.json"


def _conf(text: str) -> dict:
    items = run_checklist(text, "lease")["items"]
    return next(i for i in items if i["id"] == "confidentiality")


def test_conf_item_declared_with_signal_gate() -> None:
    """新项存在、挂 E 段、声明 signal_gate（词族非空）。"""
    item = next(i for i in load_checklist("lease")["items"]
                if i["id"] == "confidentiality")
    assert item["segment"] == "E"
    assert item["signal_gate"]["patterns"]
    assert item["signal_gate"]["na_note"] == "本卷未见保密栏目，本类不适用"


def test_no_signal_judged_na() -> None:
    """特化二：正文+表格零命中 → 本类不适用（不扣分、不旗标）。"""
    it = _conf("第一条 租赁物\n甲方将房屋出租给乙方使用。\n第二条 租金\n每月五千元。")
    assert it["status"] == "本类不适用"
    assert it["category_na"] is True
    assert it["note"] == "本卷未见保密栏目，本类不适用"
    assert it["blank_flag"] is False


@pytest.mark.parametrize("text,expect_status,expect_na,expect_blank", [
    # 实质义务（GF-2025-2614 第十二条形态）→ 通过，无旗标
    ("第十二条 保密要求\n甲乙双方对知悉的对方商业秘密均不得泄露，应当承担保密责任。",
     "通过", False, False),
    # 悬置（另行协商）→ 需关注
    ("保密要求：另行协商", "需关注", False, False),
    # 否认（特化一：普通档需关注，heuristic 不升 hardline）
    ("保密要求：无", "需关注", False, False),
    # 整栏留白（栏目标+冒号+下划线，无义务主干）→ 未找到+blank_flag
    ("第五条 保密条款：＿＿＿＿＿＿", "未找到", False, True),
    # 半留白（特化三：义务句完整只扩展栏留白）→ 通过且不旗标
    ("第十二条 保密要求\n甲、乙双方对知悉的对方的商业秘密及双方约定的其他保密信息"
     "即      ，无论本合同是否成立，均不得泄露或者不正当地使用。",
     "通过", False, False),
])
def test_four_states_and_half_blank(text: str, expect_status: str,
                                    expect_na: bool, expect_blank: bool) -> None:
    it = _conf(text)
    assert it["status"] == expect_status
    assert it["category_na"] is expect_na
    assert it["blank_flag"] is expect_blank


def test_denial_never_climbs_to_hardline() -> None:
    """特化一：否认判需关注且 rule_class=heuristic（异议层不得获红线资格）。"""
    it = _conf("保密要求：双方确认无保密要求。")
    assert it["status"] == "需关注"
    assert it["rule_class"] == "heuristic"


@pytest.mark.parametrize("text,expect_status,expect_blank", [
    # codex P1-1：门禁=下游信号超集——否认词/义务句词单独在场不得被门挡成 na
    ("双方约定对本项目技术资料无需保密。", "需关注", False),
    ("双方对知悉的商业秘密不得泄露。", "通过", False),
    # codex P1-2：全角下划线占位不是实质内容 → 未找到+旗标
    ("保密要求：＿＿＿＿＿＿", "未找到", True),
    # codex P1-3：裸词「保密责任/保密义务」+冒号非实质后缀不得洗成通过
    ("保密责任：无", "需关注", False),
    ("保密责任：另行协商", "需关注", False),
    ("保密义务：＿＿＿＿", "未找到", True),
    # codex P2：悬置/否认标签组含全部栏目词（保密条款/保密责任形态）
    ("保密条款：另行协商", "需关注", False),
    ("保密条款：无", "需关注", False),
    # 回归护栏：跨行实质义务形态仍判通过（「保密义务如下：\n承担保密责任」）
    ("保密义务如下：\n承担保密责任", "通过", False),
    # 冒号+实质内容仍放行（裸词禁行不误伤）
    ("保密责任：双方对在履行中知悉的对方资料予以保密", "通过", False),
])
def test_codex_gate_superset_and_label_guards(text: str, expect_status: str,
                                              expect_blank: bool) -> None:
    it = _conf(text)
    assert it["status"] == expect_status, f"{text!r} 期望 {expect_status}，实际 {it['status']}"
    assert it["blank_flag"] is expect_blank


def test_gate_whitelist_on_replay_corpus() -> None:
    """翻转白名单门禁：15 份回放卷仅第 1 卷出「通过」，其余全部本类不适用。

    覆盖边界如实交代：回放卷 03-15 为 git 排除的本机语料（上游校准期决定），
    CI 只覆盖已入库的 01/02——本机跑批才是全量 15 卷口径。"""
    vols = sorted(REPLAY.glob("lease_replay_[0-9][0-9].txt"))
    assert vols, "回放卷一个都不在=语料挂载异常，门禁空转必须报红"
    for p in vols:
        it = _conf(p.read_text(encoding="utf-8"))
        if p.name == "lease_replay_01.txt":
            assert it["status"] == "通过", f"{p.name} 期望通过，实际 {it['status']}"
            assert it["blank_flag"] is False
        else:
            assert it["status"] == "本类不适用", f"{p.name} 期望不适用，实际 {it['status']}"
            assert it["category_na"] is True


def test_zero_flip_against_frozen_baseline() -> None:
    """33 份语料既有判定零翻转（基线=改码前冻结，字段逐项对照；
    lease 卷排除新项 confidentiality，其余品类原样全对照）。

    覆盖边界：回放卷 03-15 为 git 排除的本机语料，源文件缺失（CI）时
    跳过该卷对照——本机跑批即全量 33 份口径；跳过量在输出中留痕。"""
    baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    assert len(baseline) == 33
    skipped: list[str] = []
    checked = 0
    for key, old in baseline.items():
        if key.startswith("replay/"):
            src = REPLAY / key.split("/", 1)[1]
            if not src.exists():
                skipped.append(key)
                continue
            text = src.read_text(encoding="utf-8")
            cat = "lease"
        else:
            p = REPO / "fixtures-real" / key.split("/", 1)[1]
            text = None  # 延迟读取（docx 走生产抽取通道）
            cat = old["category"]
        checked += 1
        drop = {"confidentiality"} if cat == "lease" else set()
        if text is None:
            from app.services.extract import extract_text
            raw = p.read_bytes()
            text = extract_text(p.name, raw)
        new_items = {i["id"]: i for i in run_checklist(text, cat)["items"]
                     if i["id"] not in drop}
        for o in old["items"]:
            n = new_items.get(o["id"])
            assert n is not None, f"{key}/{o['id']} 项消失"
            assert (n["status"], n["blank_flag"], n["category_na"]) == \
                (o["status"], o["blank_flag"], o["category_na"]), \
                f"{key}/{o['id']} 翻转: {o['status']} -> {n['status']}"
    # 至少对照到已入库的 20 份（33-13 本机语料）；跳过清单留痕不静默
    assert checked >= 20, f"零翻转对照量异常：仅 {checked} 份"
    print(f"zero-flip checked={checked} skipped(local-only)={len(skipped)}: {skipped}")


def test_no_signal_gate_in_other_categories() -> None:
    """结构零外溢：signal_gate 只挂租赁新项，其他品类任何 item 不得声明。"""
    for cat in ("procurement", "nda", "technical_service"):
        for item in load_checklist(cat)["items"]:
            assert "signal_gate" not in item, \
                f"{cat}/{item['id']} 不应声明 signal_gate"


def test_bad_signal_gate_config_fails_loud() -> None:
    """fail-closed：已配置但词族空/格式非法/正则坏 → 校验必报错（不静默放行）。"""
    base_cfg = load_checklist("lease")
    item = next(i for i in base_cfg["items"] if i["id"] == "confidentiality")
    bad_gates = [
        {"patterns": []},                      # 空词族
        {"patterns": "保密要求"},               # 词族不是列表
        {"patterns": ["保密要求", "(保密"]},    # 非法正则
        {"patterns": ["保密要求"], "na_note": 123},  # na_note 非字符串
        "保密要求",                             # 整体不是 dict
    ]
    for gate in bad_gates:
        cfg = copy.deepcopy(base_cfg)
        bad_item = next(i for i in cfg["items"] if i["id"] == "confidentiality")
        bad_item["signal_gate"] = copy.copy(gate)
        assert _signal_gate_problems("lease", "confidentiality", gate), \
            f"坏配置 {gate!r} 必须被拦截"
    # 正常配置零问题
    assert _signal_gate_problems("lease", "confidentiality",
                                 item["signal_gate"]) == []
