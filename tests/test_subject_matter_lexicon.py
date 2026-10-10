# -*- coding: utf-8 -*-
"""A3 标的语义域词表落地测试（金标裁决 v2.1 第 3 条；A1 验尸根因①）。

一期 pass 词表新增 11 个三/四字安全词；**明示不收裸「标的」**——A3 验尸
实锤两个跨词假命中同族（food「超标的」/energy「目标的」），误放行的假绿
比漏报更伤。本文件把两个假命中形态固化进测试：未来若有人收编裸「标的」，
必须先让这两条假命中形态过得了关（现实中过不了——这就是设计意图）。
"""
from __future__ import annotations

import pytest

from app.services import checklist
from app.services.extract import extract_text


def _sm(text: str) -> dict:
    out = checklist.run_checklist(text, category="procurement")
    return next(i for i in out["items"] if i["id"] == "subject_matter")


# ---------- 11 词命中（每词一条正例，existence 语义） ----------

@pytest.mark.parametrize("word", [
    "品名", "材料名称", "货物名称", "标的物", "项目名称", "承揽项目",
    "服务内容", "服务范围", "工作内容", "质量标准", "技术标准",
])
def test_new_lexicon_words_pass(word: str) -> None:
    item = _sm(f"合同第一条：{word}由双方确认后签署。")
    assert item["status"] == "通过", f"词表词 {word!r} 未命中"


def test_legacy_words_still_pass() -> None:
    """原 5 词照旧（并集不是替换）。"""
    item = _sm("设备规格参数详见配置清单。")
    assert item["status"] == "通过"


# ---------- 裸「标的」假命中免疫（A3 验尸实锤两形态固化） ----------

def test_chaobiaodi_false_hit_immunity() -> None:
    """food 实况形态：「凡是超标的蔬菜水果」——「超标+的」不得放行标的。"""
    item = _sm("凡是超标的蔬菜水果品种严禁配送，企业须对蔬菜和水果进行抽样检测。")
    assert item["status"] == "未找到"


def test_mubiaodi_false_hit_immunity() -> None:
    """energy 实况形态：「节能降碳目标的实现」——「目标+的」不得放行标的。"""
    item = _sm("乙方应配合甲方开展节能宣传工作，以保证节能降碳目标的实现。")
    assert item["status"] == "未找到"


def test_biaodiwu_passes() -> None:
    """「标的物」三字词安全命中（不会被跨词撞出）。"""
    item = _sm("标的物清单见附件一，规格型号以附件为准。")
    assert item["status"] == "通过"


# ---------- 生产入口重放（6 份授权翻转 + 2 份保持未找到） ----------

@pytest.mark.parametrize("fname", [
    "food-procurement-xinjiang-2025.docx",
    "agri-produce-sale-samr-2025.docx",
    "data-processing-service-2025.docx",
    "energy-hosting-service-2026.docx",
    "raw-milk-purchase-2016.docx",
    "work-contract-gf-2000.docx",
])
def test_authorized_flips_pass_via_production_entry(fname: str) -> None:
    """6 份授权翻转走生产全链路（抽取+引擎）确认通过——与门禁白名单一致。"""
    raw = open(f"fixtures-real/{fname}", "rb").read()
    text = extract_text(fname, raw)
    assert _sm(text)["status"] == "通过", f"{fname} 应翻转为通过"


@pytest.mark.parametrize("fname", [
    "cement-sale-gf-2008.docx",
    "mandate-contract-samr-2025.docx",
])
def test_blank_templates_stay_not_found(fname: str) -> None:
    """空白模板/无信号合同保持未找到（cement 纯空白 0 命中——合理判定不冤枉）。"""
    raw = open(f"fixtures-real/{fname}", "rb").read()
    text = extract_text(fname, raw)
    assert _sm(text)["status"] == "未找到"


# ---------- need_attention 不稀释 ----------

def test_office_vague_config_still_attention() -> None:
    """办公配置簇 need_attention 照旧（新词只进 pass，不稀释风险识别）。"""
    item = _sm("设备按办公主流配置提供，满足日常办公使用。")
    assert item["status"] == "需关注"
