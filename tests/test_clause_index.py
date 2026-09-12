"""条款索引单测（阶段 1.1）：切分变体、回退策略、坐标往返、id 稳定、item 映射。"""
from __future__ import annotations

import pytest

from app.services.clause_index import build_clause_index, map_items_to_clauses


def _ids(index: dict) -> list[str]:
    return [c["id"] for c in index["clauses"]]


# ---------- numbered 策略 ----------

def test_numbered_chinese_numerals_split():
    text = "前言内容。\n第一条 付款\n货款验收合格后支付。\n第二条 违约责任\n任何一方违约应赔偿损失。"
    idx = build_clause_index(text)
    assert idx["strategy"] == "numbered"
    assert idx["count"] == 2
    assert _ids(idx) == ["c01", "c02"]
    assert idx["clauses"][0]["heading"].startswith("第一条")
    # 坐标往返：切片必须还原条款原文
    c1 = idx["clauses"][0]
    assert text[c1["start"] : c1["end"]].startswith("第一条 付款")
    assert "货款验收合格后支付" in text[c1["start"] : c1["end"]]
    assert "违约责任" not in text[c1["start"] : c1["end"]]


def test_numbered_arabic_and_punctuation_variants():
    text = "第1条、价款\n含税总价。\n第12条.交付\n五日内交付。\n第十三条：争议\n向法院起诉。"
    idx = build_clause_index(text)
    assert idx["strategy"] == "numbered"
    assert idx["count"] == 3


def test_numbered_heading_captures_title_line():
    text = "第一条 押金\n押金不予退还。\n第二条 租期\n租期一年。"
    idx = build_clause_index(text)
    assert idx["clauses"][0]["heading"] == "第一条 押金"


def test_titled_heading_counts_as_clause():
    text = "第一条 甲\n内容存在。\n第二条 乙\n\n第三条 丙\n也有内容。"
    idx = build_clause_index(text)
    # 「第二条 乙」：标题 token 后同行还有标题文字「乙」→ 是真条款（审计二轮
    # 修复：单行条款不得因「标题行之后零正文」被吞）；真正全空的第X条才跳过
    assert idx["count"] == 3
    assert any("第二条 乙" in c["heading"] for c in idx["clauses"])


# ---------- paragraph 回退 ----------

def test_paragraph_fallback_for_flow_text():
    text = "这是一份没有条款编号的合同。" + "甲方应当付款。" * 30 + "\n" + "乙方应当交付货物。" * 30
    idx = build_clause_index(text)
    assert idx["strategy"] == "paragraph"
    assert idx["count"] >= 1
    for c in idx["clauses"]:
        assert 0 <= c["start"] < c["end"] <= len(text)


def test_paragraph_fallback_when_single_heading():
    """仅 1 个编号条款 → 流水型文本，降级 paragraph。"""
    text = "第一条 甲\n" + "正文游离在编号体系之外。" * 60
    idx = build_clause_index(text)
    assert idx["strategy"] == "paragraph"
    assert idx["count"] >= 1


def test_numbered_preamble_becomes_first_clause():
    """「第一条」之前的当事人信息/鉴于条款建档为 c01，不留在索引之外。"""
    preamble = "采购合同（编号：2026-001）。甲方：某某科技有限公司。乙方：某某贸易有限责任公司。"
    text = preamble + "\n第一条 标的\n采购办公电脑。\n第二条 价款\n含税拾万元。"
    idx = build_clause_index(text)
    assert idx["strategy"] == "numbered"
    assert idx["count"] == 3
    assert text[idx["clauses"][0]["start"] : idx["clauses"][0]["end"]].rstrip() == preamble


def test_paragraph_offsets_roundtrip():
    text = "段落甲。" * 300 + "\n" + "段落乙。" * 300
    idx = build_clause_index(text)
    assert idx["strategy"] == "paragraph"
    assert idx["count"] >= 2, "1200 字上限应把 1800 字文本切成多桶"
    for c in idx["clauses"]:
        assert text[c["start"] : c["end"]], "切片非空"


def test_crlf_line_endings_still_indexed():
    """Windows CRLF 语料（真实 docx/txt 高发）：锚点是逐段子串，不依赖分隔符形态。"""
    text = "房屋租赁合同\r\n出租方（甲方）：某某置业有限公司。\r\n承租方（乙方）：某某科技有限公司。\r\n" + "租赁相关约定内容。" * 40
    idx = build_clause_index(text)
    assert idx["strategy"] == "paragraph"
    assert idx["count"] >= 1, "CRLF 换行不得导致整桶定位失败（lease_sample 实测缺陷）"
    assert idx["clauses"][0]["start"] == 0


def test_repeated_content_buckets_never_collapse():
    """门禁 P1-1 回归探针：重复内容文本（模板句×100）桶坐标不得塌缩重叠。"""
    text = "甲方应按约供货，逾期每日按千分之一支付违约金。" * 100  # ≈2300 字，无换行
    idx = build_clause_index(text)
    assert idx["count"] >= 2
    clauses = idx["clauses"]
    covered = 0
    prev_end = -1
    for c in clauses:
        assert c["start"] >= prev_end, "桶区间必须有序且互不重叠（塌缩=分段阅读静默漏读）"
        covered += c["end"] - c["start"]
        prev_end = c["end"]
    assert covered >= 0.9 * len(text), "桶并集覆盖率不得低于 90%"
    # 坐标必须落在合法范围
    for c in clauses:
        assert 0 <= c["start"] < c["end"] <= len(text)


def test_fullwidth_indent_and_digits_headings():
    """门禁 P2-1 回归探针：全角空格缩进标题 + 全角数字编号（中文 Word 排版高发）。"""
    text = "　第一条 甲\n内容一。\n第１条 乙\n内容二。\n　　第三条 丙\n内容三。"
    idx = build_clause_index(text)
    assert idx["strategy"] == "numbered", "全角缩进/全角数字编号不得退化为 paragraph 伪桶"
    assert idx["count"] == 3


# ---------- 边界 ----------

def test_empty_text_returns_empty_index():
    for text in ("", "   \n  "):
        idx = build_clause_index(text)
        assert idx["count"] == 0
        assert idx["clauses"] == []


def test_single_short_flow_text_single_bucket():
    idx = build_clause_index("一句流水合同，无编号无换行。")
    assert idx["strategy"] == "paragraph"
    assert idx["count"] == 1
    assert idx["clauses"][0]["start"] == 0


def test_oversize_single_paragraph_hard_split():
    text = "超" * 3000
    idx = build_clause_index(text)
    assert idx["count"] == 3, "3000 字 / 1200 上限 → 3 桶"


def test_same_text_builds_stable_ids():
    text = "第一条 甲\n内容一。\n第二条 乙\n内容二。"
    assert build_clause_index(text) == build_clause_index(text)


# ---------- map_items_to_clauses ----------

def _backtoback_doc() -> tuple[str, dict]:
    text = (
        "总则说明。双方经协商一致签订本合同。\n"
        "第一条 标的\n采购办公电脑一批。\n"
        "第二条 价款\n含税总价拾万元。\n"
        "第三条 交付\n合同签订后三十日内交付。\n"
        "第四条 验收\n货到七日内验收。\n"
        "第五条 付款方式\n甲方在收到业主支付款项后，再向乙方支付货款。\n"
        "第六条 违约责任\n逾期每日按千分之一计违约金。\n"
    )
    return text, build_clause_index(text)


def test_map_items_via_hits_finds_middle_clause():
    """验收锚点：中段条款（背靠背付款）必须能定位到正确条款，而不是头尾。"""
    text, idx = _backtoback_doc()
    payment_clause = next(c for c in idx["clauses"] if "付款方式" in c["heading"])
    items = [{"id": "payment", "hits": ["收到业主支付款项后"], "quote": "…甲方在收到业主支付款项后，再向乙方支付货款。"}]
    map_items_to_clauses(items, idx, text)
    assert items[0]["clause_ids"] == [payment_clause["id"]]


def test_map_items_falls_back_to_normalized_quote():
    text, idx = _backtoback_doc()
    target = next(c for c in idx["clauses"] if "违约责任" in c["heading"])
    # hits 为空，quote 带 … 装饰与换行干扰 → 走压缩匹配回退
    items = [{"id": "breach", "hits": [], "quote": "…逾期每日按\n千分之一计违约金。"}]
    map_items_to_clauses(items, idx, text)
    assert items[0]["clause_ids"] == [target["id"]]


def test_map_items_multiple_hits_union_sorted():
    text, idx = _backtoback_doc()
    first = idx["clauses"][0]
    last = idx["clauses"][-1]
    items = [{"id": "x", "hits": ["采购办公电脑一批", "逾期每日按千分之一"], "quote": ""}]
    map_items_to_clauses(items, idx, text)
    assert items[0]["clause_ids"] == sorted([first["id"], last["id"]])


def test_map_items_unlocatable_returns_empty():
    text, idx = _backtoback_doc()
    items = [
        {"id": "missing", "hits": [], "quote": ""},  # 未找到型：无锚点
        {"id": "ghost", "hits": ["合同里根本不存在的字句"], "quote": "不存在的引用"},
    ]
    map_items_to_clauses(items, idx, text)
    assert items[0]["clause_ids"] == []
    assert items[1]["clause_ids"] == []


def test_map_items_never_raises():
    text, idx = _backtoback_doc()
    # 恶意输入：结构残缺也要静默降级，绝不让审查失败
    items = [{"id": "x", "hits": None, "quote": None}]  # type: ignore[list-item]
    map_items_to_clauses(items, idx, text)
    assert items[0]["clause_ids"] == []
    map_items_to_clauses([{"id": "y"}], {"clauses": None}, text)  # type: ignore[dict-item]
    map_items_to_clauses([{"id": "z"}], {"clauses": [{"start": None}]}, "text")  # type: ignore[dict-item]


def test_map_items_does_not_touch_existing_fields():
    """红线：item 既有字段一字不动（item.id 是三方 join key）。"""
    text, idx = _backtoback_doc()
    item = {"id": "payment", "name": "付款", "status": "需关注", "hits": ["收到业主支付款项后"], "quote": "q"}
    snapshot = {k: v for k, v in item.items()}
    map_items_to_clauses([item], idx, text)
    for k, v in snapshot.items():
        assert item[k] == v
    assert set(item.keys()) == {*snapshot.keys(), "clause_ids", "primary_clause_id"}


@pytest.mark.parametrize("strategy_text", ["", "无编号短句"])
def test_map_items_with_empty_index(strategy_text):
    items = [{"id": "a", "hits": ["x"], "quote": ""}]
    map_items_to_clauses(items, build_clause_index(strategy_text), strategy_text)
    assert items[0]["clause_ids"] == []
