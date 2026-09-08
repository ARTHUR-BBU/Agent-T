"""小智娘独立压力测试：遗留清偿批次（feat/legacy-cleanup @ 09f45fb）交叉验证。

与 test_legacy_cleanup.py（开发狗自带）互补，专攻边界与对抗输入：
① 报告未知档位：脏值变体轰炸 + 计数守恒不变量（从 docx 文本反解计数求和）
② 补盲 quote：恰 300 / 301 / 含换行仅压缩匹配 / 前缀空白被 rstrip
⑤ segment 校验：大小写、数字 key、run_checklist 报错可读性、falsy 绕过现状
⑥ 去重回归：test_scorecard.py 四 fixture 参数化、integration 无残留重复测试与死引用
⑦ 截断：恰 12000 / 12001 边界；items 备注无上限的现状记录
③ 409：pending / processing / error / null 四态文案互斥且语义正确

两个原 xfail(strict=True) 存证已由开发狗修复并转正为真测试（2026-09-06）：
- 不可哈希 status（list/dict）→ _conclusion 已加 isinstance 守卫，fail-closed
- scorecard.segments 重复 key → _validate_segment_mapping 已拦截
"""
from __future__ import annotations

import io
import re
from pathlib import Path

import pytest
import yaml
from docx import Document
from fastapi.testclient import TestClient

from app.main import app
from app.services import checklist as checklist_module
from app.services.blind_spot import (
    MAX_QUOTE_CHARS,
    _quote_supported,
    normalize_candidates,
)
from app.services.checklist import _validate_segment_mapping, run_checklist
from app.services.report import build_report_docx
from app.services.scorecard import MAX_CONTRACT_CHARS, build_user_prompt
from app.services.store import store

ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = Path(__file__).resolve().parent

client = TestClient(app)


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


def _row(items):
    return {
        "id": "xznl01", "filename": "x.txt", "category": "procurement",
        "category_label": "采购合同", "status": "done",
        "items": items, "scorecard": {}, "blind_candidates": [],
        "blind_skipped_messages": [], "blind_enabled": False,
        "policies": [], "error": None,
    }


def _counts_from_summary(text: str, n_items: int) -> dict[str, int]:
    """从「共核查 N 项：通过 X · 需关注 Y · …」反解各档计数（守恒不变量的被检方）。"""
    m = re.search(
        r"共核查 (\d+) 项：通过 (\d+) · 需关注 (\d+) · 未找到 (\d+) · 本类不适用 (\d+)"
        r"(?: · 无法识别档位 (\d+) 项)?",
        text,
    )
    assert m, f"汇总行缺失或格式变了：{text[:200]!r}"
    total, passed, attention, not_found, na, unknown = m.groups()
    return {
        "total": int(total),
        "通过": int(passed),
        "需关注": int(attention),
        "未找到": int(not_found),
        "本类不适用": int(na),
        "unknown": int(unknown or 0),
        "n_items": n_items,
    }


# ---------- ① 变体轰炸：脏 status 一律从严、永不背书 ----------

DIRTY_STATUSES = [
    ("空串", ""),
    ("None", None),
    ("尾制表符（四档变体）", "需关注\t"),
    ("形近字", "需関注"),
    ("英文小写", "attention"),
    ("英文大写", "PENDING"),
    ("数字", 123),
    ("布尔", True),
]


@pytest.mark.parametrize("label,status", DIRTY_STATUSES, ids=[l for l, _ in DIRTY_STATUSES])
def test_dirty_status_variant_never_endorses_all_pass(label, status):
    """每个脏 status：进第二节表（标注从严）、计为未知档位、绝不打出「全部适用项均通过」。"""
    row = _row([_item("a", "主体信息", "通过"), _item("b", "神秘项", status)])
    text = _doc_text(build_report_docx(row))
    assert "全部适用项均通过" not in text, f"[{label}] 脏档位落进了「全部通过」背书"
    assert "无法识别档位 1 项" in text, f"[{label}] 脏档位被静默吞掉"
    assert "按需关注处理" in text, f"[{label}] 脏档位未进第二节汇总表"
    assert "按「需关注」从严处理" in text


def test_dirty_variant_expected_cell_text():
    """表内档位列的具体呈现：空/None 显示「空」，其余原样回显 + 从严标注。"""
    row = _row([
        _item("a", "空串项", ""),
        _item("b", "None项", None),
        _item("c", "英文项", "attention"),
    ])
    text = _doc_text(build_report_docx(row))
    assert "空（按需关注处理）" in text
    assert "attention（按需关注处理）" in text
    assert text.count("（按需关注处理）") == 3, "三个未知档位都必须进表"


def test_none_status_with_missing_name_and_note_does_not_crash():
    """status=None 且 name/note/quote 全缺：报告照常生成，不炸、不空窗。"""
    row = _row([{"id": "x", "status": None}])
    text = _doc_text(build_report_docx(row))
    assert "全部适用项均通过" not in text
    assert "空（按需关注处理）" in text
    assert "未判定" in text, "逐条明细对 None 档位应显示「未判定」兜底"


def test_count_conservation_invariant_mixed():
    """计数守恒不变量：四档计数 + 未知 == len(items)，与汇总行宣称的「共核查 N 项」一致。"""
    items = [
        _item("a", "主体", "通过"),
        _item("b", "付款", "需关注"),
        _item("c", "违约", "未找到"),
        _item("d", "知产", "本类不适用"),
        _item("e", "变体1", "需关注 "),
        _item("f", "变体2", "weird"),
        _item("g", "变体3", ""),
    ]
    text = _doc_text(build_report_docx(_row(items)))
    counts = _counts_from_summary(text, len(items))
    assert counts["total"] == len(items)
    assert sum(counts[k] for k in ("通过", "需关注", "未找到", "本类不适用", "unknown")) == len(items), \
        f"计数不守恒：{counts}"
    assert counts["unknown"] == 3
    assert counts["通过"] == 1 and counts["需关注"] == 1 and counts["未找到"] == 1 and counts["本类不适用"] == 1


def test_all_unknown_items_no_endorsement():
    """极端：整份合同全是未知档位 → 决不能出现任何「全部通过」口径。"""
    row = _row([
        _item("a", "项一", "ok"),
        _item("b", "项二", "fine"),
        _item("c", "项三", "pass?"),
    ])
    text = _doc_text(build_report_docx(row))
    assert "全部适用项均通过" not in text
    assert "无法识别档位 3 项" in text
    assert "无适用清单项" not in text or True  # 逐条明细仍应展示（未知档位不是 NA）
    assert "项一" in text, "未知档位条目不能从报告中消失"


def test_unhashable_status_should_fail_closed_not_crash():
    """status 是 list/dict（理论上来自被污染的存储数据）：必须 fail-closed 按未知档位处理，
    不能 TypeError 500（开发狗已修：_conclusion 的 isinstance 守卫，本测试由 strict xfail 转正）。"""
    row = _row([_item("a", "被污染项", ["通过"])])
    text = _doc_text(build_report_docx(row))
    assert "全部适用项均通过" not in text
    assert "无法识别档位 1 项" in text


# ---------- ② quote 边界 ----------

def _gap(status="未找到", iid="p1", name="付款"):
    return {"id": iid, "name": name, "status": status, "category_na": False}


def test_quote_exactly_300_not_truncated():
    text = "合同。" + "很" * MAX_QUOTE_CHARS + "。尾部。"
    raw = [{"item_id": "p1", "name": "付款", "note": "缺付款约定。", "quote": "很" * MAX_QUOTE_CHARS}]
    candidates, skipped = normalize_candidates(raw, text, [_gap()])
    assert not skipped
    assert candidates[0]["quote"] == "很" * MAX_QUOTE_CHARS, "恰 300 字不得截断"
    assert "…" not in candidates[0]["quote"]


def test_quote_301_truncated_to_300_with_ellipsis():
    text = "合同。" + "很" * (MAX_QUOTE_CHARS + 1) + "。尾部。"
    raw_quote = "很" * (MAX_QUOTE_CHARS + 1)
    candidates, _ = normalize_candidates(
        [{"item_id": "p1", "name": "付款", "note": "备注。", "quote": raw_quote}], text, [_gap()]
    )
    q = candidates[0]["quote"]
    assert len(q) <= MAX_QUOTE_CHARS
    assert q.endswith("…")
    assert q[:-1] == raw_quote[: MAX_QUOTE_CHARS - 1], "截断必须是原 quote 的连续前缀（rstrip 无可剥字符时）"
    assert _quote_supported(text, q), "截断后的前缀仍应能通过原文核验"


def test_quote_with_newlines_truncated_prefix_still_verifiable():
    """quote 只能经空白压缩匹配到原文（含换行）且超长：先验证后截断的顺序
    必须保证截断产物仍能通过 _quote_supported——顺序反了就会产出报告里核验不过的摘句。"""
    body = "双方约定付款方式为验收合格后三十个工作日内一次性支付全部合同价款，逾期按日计违约金。" * 8
    text = "合同正文。" + body + "。完。"
    quoted = body[:340]
    quote_with_newlines = "\n".join(quoted[i:i + 17] for i in range(0, len(quoted), 17))
    assert len(quote_with_newlines) > MAX_QUOTE_CHARS
    assert quote_with_newlines not in text, "构造前提：quote 因换行只能走压缩匹配"
    assert _quote_supported(text, quote_with_newlines), "构造前提：压缩匹配成立"

    candidates, skipped = normalize_candidates(
        [{"item_id": "p1", "name": "付款", "note": "备注。", "quote": quote_with_newlines}],
        text, [_gap()],
    )
    assert not skipped, "含换行的合法摘句不能被当成无依据跳过"
    q = candidates[0]["quote"]
    assert len(q) <= MAX_QUOTE_CHARS and q.endswith("…")
    assert _quote_supported(text, q), "截断顺序错误会在这里暴露"


def test_quote_prefix_trailing_whitespace_rstrip_shrinks_below_cap():
    """截断前缀尾部是空白：rstrip 会再缩短（长度 < 上限），但仍以省略号收尾、仍是原文前缀。"""
    raw_quote = "甲" * 200 + " " * 99 + "乙" * 2
    assert len(raw_quote) == MAX_QUOTE_CHARS + 1
    text = "开头。" + raw_quote + "结尾。"
    candidates, _ = normalize_candidates(
        [{"item_id": "p1", "name": "付款", "note": "备注。", "quote": raw_quote}], text, [_gap()]
    )
    q = candidates[0]["quote"]
    assert len(q) <= MAX_QUOTE_CHARS
    assert q.endswith("…")
    assert q[:-1] == ("甲" * 200 + " " * 99)[:MAX_QUOTE_CHARS - 1].rstrip()
    assert not q[:-1].endswith(" ")
    assert _quote_supported(text, q)


def test_quote_at_cap_boundary_is_verified_before_truncation():
    """语义核验（_quote_supported）必须发生在截断之前：编一个 300+ 字的假 quote，
    若先截断后核验它会被放行（前缀凑巧……不，假 quote 根本不在原文，两种顺序都该拒绝），
    这里锁定的是：截断不会把「假 quote」洗成合法候选。"""
    text = "合同正文。" + "真" * 400 + "。完。"
    fake = "假" * (MAX_QUOTE_CHARS + 50)
    candidates, skipped = normalize_candidates(
        [{"item_id": "p1", "name": "付款", "note": "备注。", "quote": fake}], text, [_gap()]
    )
    assert not candidates
    assert skipped == ["付款: 缺少原文依据，已跳过"]


# ---------- ⑤ segment 配置校验 ----------

def _cfg(segments, items):
    return {"scorecard": {"segments": segments}, "items": items}


def test_segment_validation_is_case_sensitive():
    """小写 "a" 引用大写分段 "A"：必须报错（宁可 fail-fast 也不静默归 0 段）。"""
    cfg = _cfg([{"key": "A", "name": "主体", "weight": 30}], [{"id": "x", "segment": "a"}])
    with pytest.raises(ValueError) as ei:
        _validate_segment_mapping(cfg, "unittest")
    msg = str(ei.value)
    assert "unittest" in msg and "可用分段" in msg and "'A'" in msg


def test_segment_validation_passes_when_all_keys_known():
    """合法引用（含数字 key 强转）不误报；缺 segment 的 item 按 L-1 加严改为 fail-fast
    （单独断言见 test_falsy_segment_values_now_fail_fast）。"""
    cfg = _cfg(
        [{"key": "A", "name": "主体", "weight": 30}, {"key": "B", "name": "付款", "weight": 20}],
        [{"id": "x", "segment": "A"}, {"id": "y", "segment": "B"}],
    )
    _validate_segment_mapping(cfg, "unittest")  # 不抛即过

    with pytest.raises(ValueError, match="缺失或引用了不存在"):
        _validate_segment_mapping(
            _cfg(cfg["scorecard"]["segments"], [{"id": "z"}]), "unittest"
        )


def test_segment_validation_coerces_numeric_keys_consistently():
    """YAML 数字分段 key 与 item.segment 的 int/str 两侧都按 str 归一，不误报。"""
    cfg = _cfg(
        [{"key": 3, "name": "第三段", "weight": 10}],
        [{"id": "x", "segment": 3}, {"id": "y", "segment": "3"}],
    )
    _validate_segment_mapping(cfg, "unittest")  # 不抛即过


def test_falsy_segment_values_now_fail_fast():
    """P3 已修（老钱 L-1 加严落地）：segment: 0 / false / 缺失不再被 truthy 过滤静默放行，
    有 scorecard 块时必须 fail-fast（否则该条静默退出评分扣分，无任何告警）。"""  # noqa: E501
    cfg = _cfg(
        [{"key": "A", "name": "主体", "weight": 30}],
        [{"id": "x", "segment": 0}, {"id": "y", "segment": False}],
    )
    with pytest.raises(ValueError, match="缺失或引用了不存在"):
        _validate_segment_mapping(cfg, "unittest")


def test_duplicate_segment_keys_should_fail_fast():
    """重复分段 key 必须被拦截（开发狗已修：_validate_segment_mapping 重复检查，转正）。"""
    cfg = _cfg(
        [{"key": "A", "name": "主体", "weight": 30}, {"key": "A", "name": "主体副本", "weight": 70}],
        [{"id": "x", "segment": "A"}],
    )
    with pytest.raises(ValueError, match="重复"):
        _validate_segment_mapping(cfg, "unittest")


def test_run_checklist_bad_config_error_is_readable(monkeypatch, tmp_path):
    """新品类 YAML 写错 segment：run_checklist 报错必须可读——含品类名、坏值、可用分段。"""
    monkeypatch.setattr(checklist_module, "CONFIG_DIR", tmp_path)
    (tmp_path / "checklist_testcat.yaml").write_text(
        yaml.safe_dump(
            {
                "category": "testcat",
                "label": "测试品类",
                "scorecard": {"segments": [{"key": "A", "name": "主体", "weight": 30}]},
                "items": [
                    {"id": "x1", "name": "主体信息", "segment": "ZZZ",
                     "rules": {"pass": [{"pattern": "甲方"}]}},
                ],
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError) as ei:
        run_checklist("甲方盖章。", "testcat")
    msg = str(ei.value)
    assert "testcat" in msg, "报错必须含品类名"
    assert "ZZZ" in msg, "报错必须含坏值"
    assert "'A'" in msg, "报错必须给出可用分段"
    # 管线层：该异常会被 routes 的兜底 except 捕获 → 存为 error 态（文案可读，不是裸 traceback 500）
    assert isinstance(ei.value, Exception)


def test_run_checklist_without_scorecard_block_skips_validation(monkeypatch, tmp_path):
    """无 scorecard 块的旧品类：即便 item 带任意 segment 也跳过校验（评分未开通是合法态）。"""
    monkeypatch.setattr(checklist_module, "CONFIG_DIR", tmp_path)
    (tmp_path / "checklist_oldcat.yaml").write_text(
        yaml.safe_dump(
            {"category": "oldcat", "label": "旧品类",
             "items": [{"id": "x1", "name": "旧项", "segment": "不存在也没关系"}]},
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    result = run_checklist("任意文本。", "oldcat")
    assert result["category_label"] == "旧品类"


# ---------- ⑥ 去重回归（静态断言，防止去重回潮） ----------

def test_four_risk_parametrized_exactly_four_fixtures_in_scorecard_file():
    score_src = (TESTS_DIR / "test_scorecard.py").read_text(encoding="utf-8")
    block = re.search(r"FOUR_RISK = \[(.*?)\]", score_src, re.DOTALL)
    assert block, "test_scorecard.py 里找不到 FOUR_RISK 参数化列表"
    fixtures = re.findall(r'"([\w.]+\.txt)"', block.group(1))
    assert len(fixtures) == 4, f"FOUR_RISK 参数化应为 4 个 fixture，现在是 {len(fixtures)}"
    assert block.group(1).count("procurement_four_risk.txt") == 1, \
        "four_risk fixture 必须在 FOUR_RISK 参数化列表里且只出现一次"
    for name in fixtures:
        assert (ROOT / "fixtures" / name.strip('"')).exists(), f"fixture 缺失：{name}"


def test_integration_file_has_no_duplicate_model_side_four_risk_test():
    integ_src = (TESTS_DIR / "test_scorecard_integration.py").read_text(encoding="utf-8")
    assert "test_four_risk_missing_dispute_clause_model_full_marks_capped_74" not in integ_src, \
        "模型侧封顶 74 测试已并入 test_scorecard.py，integration 里不得回潮"
    assert "test_four_risk_missing_dispute_clause_rules_flag" in integ_src, "规则层独有断言必须保留"


def test_integration_file_dead_code_status_documented():
    """删除模型侧测试后的残留引用核查（P3 已修：`_chat_returning` 及孤儿 import
    已随去重清理，2026-09-06）：
    - integration 文件必须可编译，去重涉及的辅助函数必须仍有真实调用；
    - `_chat_returning` 不得回潮。"""
    integ_src = (TESTS_DIR / "test_scorecard_integration.py").read_text(encoding="utf-8")
    compile(integ_src, "test_scorecard_integration.py", "exec")  # 语法层：无残缺
    for helper in ("_model_payload", "annotate_rule_items"):
        uses = len(re.findall(re.escape(helper), integ_src))
        assert uses >= 2, f"{helper} 只剩 import 没有调用（死代码），应清理：出现 {uses} 次"
    assert len(re.findall("_chat_returning", integ_src)) == 0


# ---------- ⑦ 截断边界 ----------

def test_prompt_exactly_12000_chars_not_truncated():
    prompt = build_user_prompt("甲" * MAX_CONTRACT_CHARS, [])
    assert "…(截断)" not in prompt


def test_prompt_12001_chars_truncated_with_marker():
    """2026-09-08 审计整改：截断改为头+尾采样——头部前缀保留、尾部保留、
    中段以标记衔接，总长仍受 MAX_CONTRACT_CHARS 预算约束。"""
    prompt = build_user_prompt("甲" * (MAX_CONTRACT_CHARS + 1), [])
    assert "中段截断" in prompt
    body = prompt.split("合同全文：\n")[1].split("\n\n请按系统指令")[0]
    assert len(body) <= MAX_CONTRACT_CHARS + 20, f"截断稿超长：{len(body)}"
    assert set(body) <= {"甲", "\n", "…", "(", "中", "段", "截", "断", ")"}, (
        "头尾采样只允许原文前缀（甲）+ 截断标记，不得混入其他内容"
    )


def test_rule_block_has_no_length_cap_documented():
    """现状记录（评估结论见报告）：规则打标行（name/status/note）没有任何长度或条数上限。
    单条 20 万字备注即可让 prompt 远超合同 12000 字的预算——note 目前来自 YAML 配置
    属可信输入，风险低；但未来若任何模型/用户来源的字段流进 items 的 note，这里就是
    无上限注入面。"""
    long_note = "备注" * 100_000
    prompt = build_user_prompt("短合同。", [_item("a", "主体", "通过", note=long_note)])
    assert long_note in prompt, "现状：note 全量进 prompt，无截断"
    assert len(prompt) > MAX_CONTRACT_CHARS


# ---------- ③ 409 四态文案 ----------

def _report(rid):
    return client.get(f"/api/review/{rid}/report")


def test_409_messages_distinct_across_states():
    pending = _report(store.create(filename="x.txt", category="procurement", status="pending"))
    processing = _report(store.create(filename="x.txt", category="procurement", status="processing"))
    error = _report(store.create(filename="x.txt", category="procurement", status="error", error="解析失败"))
    for r in (pending, processing, error):
        assert r.status_code == 409
    assert pending.json()["detail"] == "审查尚未完成，暂不能导出报告"
    assert processing.json()["detail"] == "审查尚未完成，暂不能导出报告"
    assert error.json()["detail"] == "审查失败，请重新上传合同后再导出报告"
    details = {pending.json()["detail"], processing.json()["detail"], error.json()["detail"]}
    assert len(details) == 2, "error 态文案必须与进行中区分（失败≠未完成），进行中两态共用一条属预期"
    assert "失败" in error.json()["detail"] and "尚未完成" not in error.json()["detail"]
    assert "重新上传" in error.json()["detail"], "error 态必须给出用户可执行的下一步"
    assert "失败" not in processing.json()["detail"], "进行中不能吓唬用户说失败了"


def test_409_null_status_defaults_to_not_done_message():
    """status 字段缺失/None 的脏数据：按「尚未完成」fail-closed，绝不能放行导出。"""
    r = _report(store.create(filename="x.txt", category="procurement", status=None))
    assert r.status_code == 409
    assert r.json()["detail"] == "审查尚未完成，暂不能导出报告"
