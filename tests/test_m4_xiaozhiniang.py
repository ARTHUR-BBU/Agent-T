"""M4 报告导出 — 小智娘压力测试（漏报压力 / 禁语回显 / 超长文本 / 特殊字符 / 确定性 / API 边界）。

与 test_report.py 互补，只测开发狗没覆盖的恶意与边界输入：

- XZ1 漏报压力：items 全挂而 scorecard 谎报 95 分，报告必须以规则结果为主
- XZ2 禁语一致性（P3 遗留）：scorecard.summary / item.note 含禁语时报告层是否回显
  —— 实测结论：报告层未接禁语清洗（清洗只在 scorecard.py 生成层），旁路写入的
     裸 row 会原样回显。用 xfail(strict=False) 记录缺口，follow-up 修复后自动 xpass。
- XZ3 超长文本：note/quote/policy 塞 10000+ 字符不炸、字节流合法
- XZ4 特殊字符：emoji / 换行 / 引号可读回；XML 非法控制字符（\\x00 等）曾致导出
  永久 500（P1），已由 d733dac 修复，本文件为该修复的回归 + 合法空白防误伤
- XZ5 确定性：同一 row 两次生成的字节流逐位一致（python-docx 固定 zip 时间戳）
- XZ6 API 边界：503（python-docx 缺失）、Content-Disposition 中文/特殊文件名往返

无网络、无 Key、不改动任何现有文件。
"""
from __future__ import annotations

import copy
import io
from urllib.parse import quote, unquote

import pytest
from docx import Document
from fastapi.testclient import TestClient

from app.main import app
from app.services import report as report_service
from app.services.report import DEFAULT_DISCLAIMER, build_report_docx
from app.services.store import store

client = TestClient(app)


def _doc_text(data: bytes) -> str:
    """读回 docx 全部可见文本（段落 + 表格）。"""
    doc = Document(io.BytesIO(data))
    parts = [p.text for p in doc.paragraphs]
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                parts.append(cell.text)
    return "\n".join(parts)


def _item(name: str, status: str, note: str = "说明", quote: str = "摘句") -> dict:
    return {
        "id": f"it-{name}", "name": name, "status": status,
        "note": note, "quote": quote,
        "hits": [], "category_na": status == "本类不适用",
        "tag_source": "rule", "needs_confirm": False,
    }


def _base_row() -> dict:
    """不含业务语义的干净底座，各测试自行覆盖字段。"""
    return {
        "id": "xz00000001",
        "filename": "压力测试合同.pdf",
        "category": "procurement",
        "category_label": "采购合同",
        "status": "done",
        "created_at": "2026-09-06 10:30",
        "text": "（合同全文，绝不入报告）",
        "items": [],
        "scorecard": {},
        "blind_candidates": [],
        "blind_skipped_messages": [],
        "blind_skipped_reason": None,
        "blind_enabled": True,
        "policies": [],
        "error": None,
    }


# ---------- XZ1 漏报压力：模型全过、规则全挂 ----------

def test_xz1_scorecard_lies_but_rules_still_lead():
    """items 全是需关注/未找到而 scorecard 谎报 95 分：规则区必须完整、明确从属声明.

    记录现状：谎报的总分会以「参考评分」前缀回显（缓解手段仅是标签+从属句），
    报告层对 total 与规则结果无一致性校验——见交付报告 P2-1。
    """
    row = _base_row()
    row["items"] = [
        _item("付款条件", "需关注", "签约即付全款。", "三日内支付全款。"),
        _item("违约责任", "需关注", "违约金上限过低。", "违约金为一千元。"),
        _item("验收条款", "需关注", "无验收标准。", ""),
        _item("质保金", "需关注", "无质保金安排。", ""),
        _item("管辖约定", "未找到", "全文无争议解决条款。", ""),
        _item("合同期限", "未找到", "全文无期限条款。", ""),
        _item("签署栏", "未找到", "全文无签署栏。", ""),
    ]
    row["scorecard"] = {
        "available": True, "reason": None, "total": 95,
        "tier": {"label": "基本可控", "hint": ""},
        "summary": "整体风险很低，问题不大。",  # 顺带夹带 C 类禁语
        "segments": [], "caps_applied": [],  # 谎报：未封顶
        "disclaimer": "参考意见，签署前请人工复核。",
        "advisory_only": True,
    }
    text = _doc_text(build_report_docx(row))

    # 规则计数必须如实：0 通过、4 需关注、3 未找到
    assert "共核查 7 项" in text
    assert "通过 0" in text
    assert "需关注 4" in text
    assert "未找到 3" in text

    # 评分从属地位声明必须在场，且评分不得盖过规则区
    assert "各项结论以逐条规则结果为准" in text
    assert text.index("共核查 7 项") < text.index("95/100"), "规则计数先于评分出现"
    assert "逐条明细" in text and "未找到】管辖约定" in text

    # 记录现状：谎报的 95 分与「基本可控」档位确实被回显（带参考评分前缀）
    assert "参考评分：95/100（基本可控）" in text

    # 报告不得替模型圆谎：不得出现"全部通过"类表述
    assert "全部适用项均通过" not in text


# ---------- XZ2 禁语一致性（P3 已修：报告层 _scrub 二次清洗，2026-09-06 转正） ----------

def test_xz2a_forbidden_summary_not_echoed():
    """scorecard.summary 含 A/C 类禁语时，报告层应清洗后再落盘.

    开发狗 follow-up 已补报告层 _scrub（report.py 对 summary/note 复用
    scrub_forbidden），本测试由 xfail 转正为真测试。
    """
    row = _base_row()
    row["items"] = [_item("主体信息", "需关注", "主体信息不全。", "甲方信息空白。")]
    row["scorecard"] = {
        "available": True, "total": 88, "tier": {"label": "基本可控"},
        "summary": "本合同没有问题，可以放心签署。",
        "caps_applied": [], "disclaimer": "参考意见。",
    }
    text = _doc_text(build_report_docx(row))
    assert "本合同没有问题" not in text, "A 类禁语不得经报告回显"
    assert "可以放心签署" not in text, "A 类禁语不得经报告回显"


def test_xz2b_forbidden_note_not_echoed():
    """item.note / blind note 含禁语时同理（P3 已修，转正为真测试）."""
    row = _base_row()
    row["items"] = [_item("违约责任", "需关注", "虽有提示但可以忽略。", "违约金条款。")]
    row["blind_candidates"] = [{
        "id": "b1", "name": "签署与印章", "status": "需关注",
        "note": "这个条款问题不大。", "quote": "双方盖章。",
        "needs_confirm": True, "named_by_scorecard": False,
    }]
    text = _doc_text(build_report_docx(row))
    assert "虽有提示但可以忽略" not in text, "C 类禁语不得经报告回显"
    assert "问题不大" not in text, "C 类禁语不得经报告回显"


def test_xz2c_blind_candidates_stay_in_own_section():
    """补盲候选不得与规则结论混排：不进汇总表、不进逐条明细，只在第四节."""
    row = _base_row()
    row["items"] = [_item("付款条件", "需关注", "付款过急。", "三日内支付。")]
    row["blind_candidates"] = [{
        "id": "b1", "name": "补盲独有风险点", "status": "需关注",
        "note": "仅有盖章栏。", "quote": "盖章。",
        "needs_confirm": True, "named_by_scorecard": False,
    }]
    text = _doc_text(build_report_docx(row))
    sec2 = text.index("二、需关注与未找到汇总")
    sec3 = text.index("三、逐条明细")
    sec4 = text.index("四、模型补盲候选")
    assert "补盲独有风险点" not in text[sec2:sec3], "候选不得进规则汇总表"
    assert "补盲独有风险点" not in text[sec3:sec4], "候选不得混入逐条明细"
    assert text.index("补盲独有风险点") > sec4, "候选只出现在自己的节里"
    assert "需人工确认" in text[sec4:]


# ---------- XZ3 超长文本 ----------

def test_xz3_huge_texts_do_not_break_docx():
    """note/quote/policy 塞 10000+ 字符：不炸、PK 头合法、python-docx 读回完整."""
    big = "超" * 12000
    row = _base_row()
    row["items"] = [
        _item("超长条目", "需关注", "首" + big + "尾", "引" + big + "句"),
    ]
    row["policies"] = ["政" + big + "策"]
    data = build_report_docx(row)
    assert data[:2] == b"PK"
    text = _doc_text(data)
    assert "首" + "超" * 50 in text
    assert ("超" * 12000) in text, "长文本不得被截断"
    assert ("政" + "超" * 12000 + "策") in text
    assert len(data) > 20000


# ---------- XZ4 特殊字符 ----------

def test_xz4_emoji_newline_quotes_roundtrip():
    """emoji / 换行 / 各类引号：正常生成且读回不丢."""
    row = _base_row()
    tricky = "含\"双引号\"'单引号'「直角」《书名》\n第二行 🈶🎉 电子✉"
    row["items"] = [_item("特殊字符条目", "需关注", tricky, tricky)]
    row["blind_skipped_messages"] = ["跳过原因：含特殊字符 🎉"]
    text = _doc_text(build_report_docx(row))
    assert "第二行" in text
    assert "🈶" in text and "🎉" in text
    assert "「直角」" in text
    assert "含特殊字符 🎉" in text


@pytest.mark.parametrize("label,bad", [
    ("NUL", "abc\x00def"),
    ("VT", "abc\x0bdef"),
    ("ESC", "abc\x1bdef"),
    ("FF", "abc\x0cdef"),
])
def test_xz4b_xml_illegal_control_chars_are_stripped(label, bad):
    """XML 1.0 非法控制字符混入 note/quote 时不得炸报告（d733dac 修复的回归）.

    历史：抽取层（pypdf/docling/errors='replace'）残留控制字符 → lxml 抛
    ValueError → 该条审查的报告导出永久 500。现由 build_report_docx 入口
    _sanitize 单点收口剔除。本组验证：不炸、条目保留、控制字符确实被剔除。
    """
    row = _base_row()
    row["items"] = [_item(f"控制字符-{label}", "需关注", f"说明{bad}尾", f"摘{bad}句")]
    text = _doc_text(build_report_docx(row))
    assert f"控制字符-{label}" in text, "清洗后条目本身必须保留"
    assert bad not in text, "控制字符必须被剔除，不得原样落盘"


def test_xz4c_legal_whitespace_survives_sanitize():
    """清洗不得误伤合法空白：\\t \\n \\r 必须保留（docx 里转为 tab/换行）."""
    row = _base_row()
    row["items"] = [_item("空白条目", "需关注", "a\tb\nc\rd", "x\ty")]
    text = _doc_text(build_report_docx(row))
    assert "a\tb" in text
    # \r 在 docx 层规范成 <w:br/>，读回时呈现为 \n；只要没被清洗剔除即可
    assert ("c\rd" in text) or ("c\nd" in text)


# ---------- XZ5 确定性 ----------

def test_xz5_same_row_produces_identical_bytes():
    """同一 row 连续生成两次：正文内容与结构逐位一致（无随机因素混入正文）.

    开发狗修正记录：python-docx 会在 zip 条目头写入当前时间（2 秒粒度），
    两次 build 跨过边界时**容器字节**必然不同——这是库的固有行为，不是
    报告层引入的随机性。正文（段落+表格文本）才是我们承诺确定性的层面，
    改为读回比对；条目集合也必须一致（结构无漂移）。
    """
    row = _base_row()
    row["items"] = [_item("付款条件", "需关注", "付款过急。", "三日内支付。")]
    row["blind_candidates"] = [{
        "id": "b1", "name": "签署", "status": "需关注", "note": "缺签字栏。",
        "quote": "盖章。", "needs_confirm": True, "named_by_scorecard": True,
    }]
    row["scorecard"] = {"available": True, "total": 80, "tier": {"label": "基本可控"},
                        "summary": "汇总。", "caps_applied": [], "disclaimer": "参考。"}
    b1 = build_report_docx(row)
    b2 = build_report_docx(copy.deepcopy(row))
    assert _doc_text(b1) == _doc_text(b2), "报告正文必须确定性，不得混入时间戳等随机因素"
    assert _entry_names(b1) == _entry_names(b2), "docx 内部条目结构不得漂移"


def _entry_names(data: bytes) -> set[str]:
    import zipfile

    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return set(zf.namelist())


# ---------- 免责句兜底补充 ----------

def test_xz6_blank_disclaimer_falls_back_to_default():
    """disclaimer 为纯空白时也必须落默认免责句（strip 路径）."""
    row = _base_row()
    row["scorecard"] = {"available": True, "total": 80, "tier": {"label": "基本可控"},
                        "summary": "汇总。", "caps_applied": [], "disclaimer": "   \n  "}
    assert DEFAULT_DISCLAIMER in _doc_text(build_report_docx(row))


# ---------- API 边界补充 ----------

def test_xz7_api_503_when_docx_lib_missing(monkeypatch):
    """python-docx 缺失（ImportError）→ 503 可操作错误，而非 500 裸异常."""
    row = _base_row()
    row["items"] = [_item("付款条件", "需关注", "付款过急。", "三日内支付。")]
    rid = store.create(**row)

    def _boom(_row):
        raise ImportError("No module named 'docx'")

    monkeypatch.setattr(report_service, "build_report_docx", _boom)
    r = client.get(f"/api/review/{rid}/report")
    assert r.status_code == 503
    assert "python-docx" in r.json()["detail"]


def test_xz7b_api_generic_failure_is_500_without_detail_leak(monkeypatch):
    """非 ImportError 的生成异常 → 500 固定文案；异常串不得回给客户端（d733dac P2-1）."""
    rid = store.create(**_base_row())

    def _boom(_row):
        raise RuntimeError("磁盘写入失败 C:\\server\\secret\\path")

    monkeypatch.setattr(report_service, "build_report_docx", _boom)
    r = client.get(f"/api/review/{rid}/report")
    assert r.status_code == 500
    detail = r.json()["detail"]
    assert "磁盘写入失败" not in detail, "异常串不得外泄"
    assert "secret" not in detail, "实现细节不得外泄"
    assert detail == "报告生成失败，请稍后重试"


def test_xz7c_content_disposition_rfc5987_roundtrip():
    """文件名含中文/引号/emoji/空格时 Content-Disposition 走 RFC 5987 且可往返."""
    tricky = "合同\"终稿\"v2（加密） 🈶.pdf"
    row = _base_row()
    row["filename"] = tricky
    rid = store.create(**row)
    r = client.get(f"/api/review/{rid}/report")
    assert r.status_code == 200
    assert r.content[:2] == b"PK"
    cd = r.headers["content-disposition"]
    assert "attachment" in cd
    assert 'filename="report.docx"' in cd, "必须有 ASCII 兜底文件名"
    assert "filename*=UTF-8''" in cd
    encoded = cd.split("filename*=UTF-8''", 1)[1].split(";", 1)[0].strip()
    # 报告文件名格式：审查报告-{stem}-{id}.docx
    expected = quote(f"审查报告-{tricky[:-4]}-{rid}.docx")
    assert unquote(encoded) == unquote(expected), (
        f"文件名往返不一致：{unquote(encoded)!r} != {unquote(expected)!r}"
    )
    assert encoded.isascii(), "Content-Disposition 头必须是纯 ASCII（否则 latin-1 编码炸）"


def test_xz7d_report_of_row_with_weird_filename_inside_docx():
    """文件名里的换行符不炸封面、不炸头."""
    row = _base_row()
    row["filename"] = "多行\n文件名.pdf"
    rid = store.create(**row)
    r = client.get(f"/api/review/{rid}/report")
    assert r.status_code == 200
    text = _doc_text(r.content)
    assert "多行" in text
