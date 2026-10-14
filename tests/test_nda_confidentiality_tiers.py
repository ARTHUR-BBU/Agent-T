# -*- coding: utf-8 -*-
"""NDA 保密栏目四态回填测试（老钱裁定 2026-10-10：F-12 v2.1 跨品类挂账销项）。

四态（三品类统一基线，NDA 特例两处）：
  实质→通过（原有 all_of 照旧）/悬置→需关注/否认→需关注（NDA 升
  hardline：保密是主给付义务，否认=合同目的落空）/留白→**需关注+旗标**
  （NDA 特例：missing_as 抬档，比三品类基线重一档——标题党防线与留白
  共用未找到出口，拆分需引擎细分特性，施工卡挂账）。

老钱点名的最大误报雷区：标准 NDA 例外条款（公知信息/独立开发/第三方
合法取得）中的「不承担保密义务」有合法居所，unless 豁免门放行——
变异验证（删 unless 转红）见施工卡。
"""
from __future__ import annotations

import pytest

from app.services import checklist
from app.services.extract import extract_text


def _nda(text: str) -> dict:
    out = checklist.run_checklist(text, category="nda")
    return next(i for i in out["items"] if i["id"] == "confidentiality_scope")


@pytest.mark.parametrize(
    ("text", "exp_status", "exp_flag"),
    [
        # 通过档：实质义务（原有 all_of 照旧，并集不是替换）
        ("乙方对甲方披露的保密信息负有保密义务，不得向第三方披露。", "通过", False),
        # 需关注档：悬置（P2 pending 判例）
        ("保密义务：另行协商", "需关注", False),
        # 需关注档：纯否认（NDA 红线簇 hardline——保密是主给付义务）
        ("双方均不承担保密义务。", "需关注", False),
        ("本协议无需保密。", "需关注", False),
        # 通过档：例外条款+主条款并存（真实场景——unless 豁免门放行）
        (
            "乙方对甲方披露的保密信息负有保密义务，不得向第三方披露。"
            "下列信息不承担保密义务：已进入公共领域的公知信息。",
            "通过", False,
        ),
        (
            "乙方对甲方的技术资料、商业秘密严格保密。"
            "接收方独立开发获得的资料，不承担保密义务。",
            "通过", False,
        ),
        # 需关注档：例外形态裸否认（无豁免语境，unless 不放行）
        ("乙方对甲方数据不承担保密义务，可自由使用。", "需关注", False),
        # 留白档（NDA 特例：需关注+旗标，missing_as 抬档见文件头注记）
        ("保密信息范围：\n第二条 义务", "需关注", True),
        # 需关注档：标题党 NDA（全文无保密信号——missing_as 防线）
        ("本协议一式两份。", "需关注", False),
        # ---- 外审 #105 四场景固化 ----
        # P1-1：空模板双标题不得互充实质内容（标题形态负向断言）
        ("保密信息范围：\n保密义务：", "需关注", True),
        # P1-2：表格跨格悬置（tab 分支）不得被 pass 洗成通过
        ("保密信息范围\t待定\n保密义务\t另行协商", "需关注", False),
        # P1-3：例外条款点名「保密信息」主体不得满足 pass 范围组
        ("下列保密信息不承担保密义务：已进入公共领域的信息", "需关注", False),
        # P2-4：完整 NDA 的例外声明（无保密义务+豁免语境）不得误伤
        (
            "乙方对甲方披露的保密信息负有保密义务，不得向第三方披露。"
            "接收方对已公开或独立开发的信息无保密义务。",
            "通过", False,
        ),
        # 义务组积极化回归：正常「负有保密义务」不被 (?<!不) 误拦
        ("乙方对上述保密信息负有保密义务，期限五年。", "通过", False),
        # ---- 外审 #105 复核 P2：空栏着火不漏栏 ----
        # 单独「保密义务：」空栏同样着火（此前只认保密信息范围形态）
        ("保密义务：\n第二条 其他", "需关注", True),
        # 表格左格「保密义务」右格空=空占位形态，同样着火
        ("保密义务\t\n第二条 其他", "需关注", True),
        # 正例保护：下一行确有实质义务内容时不误着火
        (
            "保密义务：\n乙方对保密信息负有保密义务，不得向第三方披露。",
            "通过", False,
        ),
    ],
)
def test_nda_confidentiality_four_tiers(
    text: str, exp_status: str, exp_flag: bool
) -> None:
    item = _nda(text)
    assert item["status"] == exp_status, f"{text!r}: {item['status']} ≠ {exp_status}"
    assert bool(item.get("blank_flag")) is exp_flag, (
        f"{text!r}: blank_flag={item.get('blank_flag')} ≠ {exp_flag}")


def test_blank_flag_note_fixed_wording() -> None:
    """旗标文案固定（NDA 自有形态）。"""
    item = _nda("保密信息范围：\n第二条 义务")
    assert item["blank_flag"] is True
    assert item["note"] == "检测到保密信息范围栏目但内容空白。"


def test_exception_clause_without_main_clause_stays_attention() -> None:
    """孤立例外条款（无主条款）不构成通过——existence 语义：这段话没有
    实质保密义务约定；真实合同例外条款旁必有主条款（上一用例覆盖）。"""
    item = _nda("下列信息不承担保密义务：已进入公共领域的公知信息。")
    assert item["status"] == "需关注"


# ---------- 生产入口重放（零翻转锁死） ----------

@pytest.mark.parametrize("fname", [
    "trade-secret-nda-guangdong.docx",
    "business-cooperation-nda-guangdong.docx",
])
def test_real_nda_replay_unchanged(fname: str) -> None:
    """两份真实 NDA 重放=通过（动词表前验尸：否认簇/例外形态零命中，
    零误伤零翻转——与 17 份门禁 NDA 项基线一致）。"""
    raw = open(f"fixtures-real/{fname}", "rb").read()
    text = extract_text(fname, raw)
    item = _nda(text)
    assert item["status"] == "通过", f"{fname} 意外翻转: {item['status']}"
    assert not item.get("blank_flag")
