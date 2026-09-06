"""M3.5/M4 遗留清偿回归测试。

① 报告层未知档位往风险侧倒（计数守恒，不落「全部通过」背书）
② 补盲候选 quote 长度上限（300 字截断，前缀仍是已验证原文）
③ 409 文案区分 error 态（失败≠未完成）
⑤ checklist 配置 segment 越界 fail-fast
⑦ >12000 字截断：模型只看截断稿，规则引擎看全文
"""
from __future__ import annotations

import io

import pytest
from docx import Document

from app.services.blind_spot import MAX_QUOTE_CHARS, normalize_candidates
from app.services.checklist import _validate_segment_mapping, load_checklist, run_checklist
from app.services.report import build_report_docx
from app.services.scorecard import MAX_CONTRACT_CHARS, build_user_prompt, load_scorecard_config


def _doc_text(data: bytes) -> str:
    doc = Document(io.BytesIO(data))
    parts = [p.text for p in doc.paragraphs]
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                parts.append(cell.text)
    return "\n".join(parts)


def _item(iid, name, status, note="备注。", quote="摘句。"):
    return {"id": iid, "name": name, "status": status, "note": note, "quote": quote,
            "hits": [], "category_na": False, "tag_source": "rule", "needs_confirm": False}


# ---------- ① 未知档位往风险侧倒 ----------

def _row(items):
    return {
        "id": "legacy01", "filename": "x.txt", "category": "procurement",
        "category_label": "采购合同", "status": "done",
        "items": items, "scorecard": {}, "blind_candidates": [],
        "blind_skipped_messages": [], "blind_enabled": False,
        "policies": [], "error": None,
    }


def test_unknown_status_treated_as_attention_and_counted():
    """未知档位「需关注 」（尾空格变体）：必须出现在汇总表（标注从严口径），
    汇总行给出计数，且不得打出「全部适用项均通过」."""
    row = _row([
        _item("a", "主体信息", "通过"),
        _item("b", "付款条件", "需关注 "),
        _item("c", "违约责任", "未找到"),
    ])
    text = _doc_text(build_report_docx(row))
    assert "无法识别档位 1 项" in text, "未知档位被静默吞掉了（计数不守恒）"
    assert "按「需关注」从严处理" in text
    assert "需关注 （按需关注处理）" in text, "未知档位必须进第二节汇总表"
    assert "全部适用项均通过" not in text


def test_unknown_status_only_cannot_produce_all_pass_endorsement():
    """极端：全是未知档位 → 绝不能背书「全部通过」."""
    row = _row([_item("a", "神秘项", "weird_status")])
    text = _doc_text(build_report_docx(row))
    assert "全部适用项均通过" not in text
    assert "无法识别档位 1 项" in text
    assert "weird_status（按需关注处理）" in text


def test_status_counts_conserved():
    """计数守恒不变量：汇总行四档 + 未知 == len(items)."""
    row = _row([
        _item("a", "主体", "通过"),
        _item("b", "付款", "需关注"),
        _item("c", "违约", "未找到"),
        _item("d", "知产", "本类不适用"),
        _item("e", "神秘", "whatever"),
    ])
    text = _doc_text(build_report_docx(row))
    # 逐个计数都出现且总和=5：4 明档 + 1 未知
    assert "共核查 5 项" in text
    for fragment in ("通过 1", "需关注 1", "未找到 1", "本类不适用 1", "无法识别档位 1 项"):
        assert fragment in text


# ---------- ② 补盲候选 quote 上限 ----------

def test_blind_candidate_quote_capped():
    """万字 quote 必须截断到 MAX_QUOTE_CHARS，且截断后仍以省略号结尾."""
    text = "合同正文。" + "很" * 2000 + "。条款尾部。"
    long_quote = "很" * 1000
    raw = [{"item_id": "p1", "name": "付款", "note": "有风险。", "quote": long_quote}]
    gaps = [{"id": "p1", "name": "付款", "status": "通过", "category_na": False}]
    candidates, skipped = normalize_candidates(raw, text, gaps)
    assert len(candidates) == 1
    q = candidates[0]["quote"]
    assert len(q) == MAX_QUOTE_CHARS
    assert q.endswith("…")
    assert set(q[:-1]) <= {"很"}, "截断只能切前缀，不得引入原文没有的内容"


def test_blind_candidate_quote_under_cap_untouched():
    text = "合同正文，双方约定交付日期为签署后二十日。"
    raw = [{"item_id": "p1", "name": "期限", "note": "缺期限。", "quote": "签署后二十日"}]
    gaps = [{"id": "p1", "name": "期限", "status": "通过", "category_na": False}]
    candidates, _ = normalize_candidates(raw, text, gaps)
    assert candidates[0]["quote"] == "签署后二十日"


# ---------- ⑤ segment 配置校验 ----------

def test_segment_mapping_validation_rejects_unknown_key():
    cfg = {
        "scorecard": {"segments": [{"key": "A", "name": "A", "weight": 10}]},
        "items": [{"id": "x", "segment": "Z"}],
    }
    with pytest.raises(ValueError, match="不存在的评分卡分段"):
        _validate_segment_mapping(cfg, "unittest")


def test_segment_mapping_validation_allows_missing_scorecard_block():
    """无 scorecard 块的旧配置：跳过校验（评分未开通是合法态）."""
    cfg = {"items": [{"id": "x", "segment": "A"}]}
    _validate_segment_mapping(cfg, "unittest")  # 不抛即过


def test_real_category_configs_all_valid():
    """存量品类配置必须全过校验（防 fail-fast 误伤）."""
    for cat in ("procurement", "nda"):
        load_checklist(cat)  # 内部即走校验，配置错误会抛 ValueError
        assert load_scorecard_config(cat)["segments"], f"{cat} 评分卡分段不能为空"


# ---------- ⑦ 截断 ----------

def test_long_contract_truncated_in_prompt_with_marker():
    """>12000 字：user prompt 只带截断稿 + 明确标记，不能整篇塞给模型."""
    text = "甲" * (MAX_CONTRACT_CHARS + 3000)
    prompt = build_user_prompt(text, [_item("a", "主体", "通过")])
    assert "…(截断)" in prompt
    body = prompt.split("合同全文：\n")[1].split("\n\n请按系统指令")[0]
    assert len(body) <= MAX_CONTRACT_CHARS + 20, f"截断稿超长：{len(body)}"


def test_short_contract_not_truncated():
    text = " short contract "
    prompt = build_user_prompt(text, [])
    assert "…(截断)" not in prompt


def test_rules_see_full_text_even_when_prompt_truncated():
    """规则引擎在全文上打标（截断只影响模型看到的上下文）：
    把靶点关键词放在 12000 字之后，规则仍必须命中."""
    target = "签约后当日一次性支付全部价款，不再验收。"
    text = "甲" * (MAX_CONTRACT_CHARS + 500) + "\n" + target
    result = run_checklist(text, "procurement")
    payment = next(i for i in result["items"] if i["id"] == "payment")
    assert payment["status"] == "需关注", "截断上下文不应影响规则引擎在全文上的命中"
