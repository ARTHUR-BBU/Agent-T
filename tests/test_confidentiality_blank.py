# -*- coding: utf-8 -*-
"""A2 空壳保密标签 · 保密项三档判定测试（老钱裁定 2026-10-09 + 用户批复红线）。

口径（三档对齐 P2 期限）：
  空栏（标签后无内容）   → 未找到 + blank_flag 旗标（提示「栏目没填」，非风险警报）
  空话（另行协商/待定…） → 需关注
  实质内容（含表格跨格） → 通过（原三词保密义务/商业秘密/不得披露照旧，并集不是替换）

前置复现结论：生产 extract_text 保留表格跨格内容为「标签\\t内容」形态，
tab 只来自表格同行单元格连接、正文无 tab——正则用它区分两种填写形态。
"""
from __future__ import annotations

import io

import pytest

from app.services import checklist
from app.services.extract import extract_text


def _conf(text: str) -> dict:
    out = checklist.run_checklist(text, category="procurement")
    return next(i for i in out["items"] if i["id"] == "confidentiality")


# ---------- 三档判定（原型 14 场景走生产引擎全链路） ----------

@pytest.mark.parametrize(
    ("text", "exp_status", "exp_flag"),
    [
        # 通过档：实质内容（跨格 tab / 同格冒号 / 原三词照旧）
        ("保密要求\t双方不得向第三方披露", "通过", False),
        ("保密要求：乙方负有保密义务", "通过", False),
        ("乙方负有保密义务，不得泄露。", "通过", False),
        ("双方对商业秘密负有保护责任", "通过", False),
        ("任何一方不得披露对方技术资料", "通过", False),
        # 需关注档：空话（P2 pending 同款）
        ("保密要求：另行协商", "需关注", False),
        ("保密要求\t另行协商", "需关注", False),
        ("保密要求：待定", "需关注", False),
        # 未找到+旗标档：空栏（冒号后无内容 / 表格标签格行尾）
        ("保密要求\n", "未找到", True),
        ("第四条 时间、办法及保密要求：\n第五条 验收标准", "未找到", True),
        ("保密要求：。", "未找到", True),
        ("保密要求\n第五条 验收标准：合格", "未找到", True),
        ("保密要求\t\n交付期限\t2026年12月31日前", "未找到", True),
        # 未找到无旗标档：正文裸词（「保密要求。」是陈述不是栏目）
        ("乙方应满足甲方的保密要求。", "未找到", False),
        ("第四条 办法及保密要求。", "未找到", False),
        # 未找到无旗标档：无任何保密信号
        ("本合同一式两份", "未找到", False),
    ],
)
def test_confidentiality_three_tiers(text: str, exp_status: str, exp_flag: bool) -> None:
    item = _conf(text)
    assert item["status"] == exp_status, f"{text!r}: {item['status']} ≠ {exp_status}"
    assert bool(item.get("blank_flag")) is exp_flag, (
        f"{text!r}: blank_flag={item.get('blank_flag')} ≠ {exp_flag}")


def test_blank_flag_note_fixed_wording() -> None:
    """旗标文案固定（用户批复）：「检测到保密要求栏目但内容空白。」"""
    item = _conf("第四条 时间、办法及保密要求：\n第五条 验收标准")
    assert item["blank_flag"] is True
    assert item["note"] == "检测到保密要求栏目但内容空白。"


def test_attention_note_mentions_suspension() -> None:
    """空话档 note 必须说明「实质悬置」，不得与空栏旗标文案混淆。"""
    item = _conf("保密要求：另行协商")
    assert item["status"] == "需关注"
    assert "悬置" in item["note"]


def test_pending_beats_pass_on_mixed_text() -> None:
    """判定顺序 pending>pass：空话与实义并存时取需关注（P2 汇总优先级同款）。"""
    item = _conf("保密要求：另行协商\n另页约定：乙方负有保密义务")
    assert item["status"] == "需关注"


# ---------- 生产入口重放（金标证物固化） ----------

def test_work_contract_live_replay() -> None:
    """work-contract GF-2000 实况（A1 验尸证物）：保密要求栏冒号后留白
    → 未找到+旗标；状态与施工前一致（零翻转锁死令），仅新增旗标。"""
    raw = open("fixtures-real/work-contract-gf-2000.docx", "rb").read()
    text = extract_text("work-contract-gf-2000.docx", raw)
    item = _conf(text)
    assert item["status"] == "未找到"
    assert item["blank_flag"] is True
    assert item["note"] == "检测到保密要求栏目但内容空白。"


def test_filled_confidentiality_requirement_passes() -> None:
    """老钱强制配套②（防修复过头）：填写的保密要求必须通过——
    GF-2000 形态修复后版本（第四条内容补全）。"""
    text = (
        "第四条 定作人提供技术资料、图纸等的时间、办法及保密要求："
        "乙方对定作人提供的技术资料负有保密义务，不得向第三方披露。"
    )
    item = _conf(text)
    assert item["status"] == "通过"
    assert item["blank_flag"] is False


# ---------- docx 端到端（表格跨格场景过真实抽取路径） ----------

def _build_docx(
    tbl_rows: list[list[list[str | None]]], paras: list[str] | None = None
) -> bytes:
    from docx import Document

    doc = Document()
    for p in paras or []:
        doc.add_paragraph(p)
    for rows in tbl_rows:
        t = doc.add_table(rows=len(rows), cols=len(rows[0]))
        for i, row in enumerate(rows):
            for j, c in enumerate(row):
                if c is not None:
                    t.cell(i, j).text = c
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def test_table_adjacent_cell_filled_passes_end_to_end() -> None:
    """用户红线 1 端到端：表格左格「保密要求」+右格实质内容=已填写，必须通过；
    不得因标签后是制表符误判空白。"""
    raw = _build_docx([[["保密要求", "双方不得向第三方披露技术资料"]]])
    text = extract_text("t.docx", raw)
    item = _conf(text)
    assert item["status"] == "通过"
    assert item["blank_flag"] is False


def test_table_blank_cell_flagged_end_to_end() -> None:
    """表格标签格+右格空白=空栏：未找到+旗标（走真实抽取路径）。"""
    raw = _build_docx([[["保密要求", None]]])
    text = extract_text("t.docx", raw)
    item = _conf(text)
    assert item["status"] == "未找到"
    assert item["blank_flag"] is True


def test_table_pending_cell_attention_end_to_end() -> None:
    """表格跨格空话：未找到档之上的需关注（P2 pending 跨格形态）。"""
    raw = _build_docx([[["保密要求", "另行协商"]]])
    text = extract_text("t.docx", raw)
    item = _conf(text)
    assert item["status"] == "需关注"


# ---------- 配置校验（fail-closed） ----------

def test_invalid_blank_flag_pattern_rejected(tmp_path) -> None:
    """blank_flag_pattern 非法正则必须在 load 期报错（静默失效=旗标永不亮）。"""
    import yaml

    from app.services import checklist as cl

    cfg = {
        "version": 1,
        "items": [
            {
                "id": "confidentiality",
                "name": "保密",
                "class": "existence",
                "rules": {
                    "pass": [
                        {
                            "any_of": ["保密义务"],
                            "blank_flag_pattern": "保密要求[:：(?!x)",
                        }
                    ]
                },
            }
        ],
    }
    p = tmp_path / "checklist_bad.yaml"
    p.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    with pytest.raises(ValueError, match="blank_flag_pattern"):
        cl._validate_matchers(p, cfg)


def test_non_string_blank_flag_pattern_rejected(tmp_path) -> None:
    """blank_flag_pattern 非字符串同样 fail-closed。"""
    import yaml

    from app.services import checklist as cl

    cfg = {
        "version": 1,
        "items": [
            {
                "id": "confidentiality",
                "name": "保密",
                "class": "existence",
                "rules": {"pass": [{"any_of": ["保密义务"], "blank_flag_pattern": 123}]},
            }
        ],
    }
    p = tmp_path / "checklist_bad2.yaml"
    p.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    with pytest.raises(ValueError, match="blank_flag_pattern"):
        cl._validate_matchers(p, cfg)
