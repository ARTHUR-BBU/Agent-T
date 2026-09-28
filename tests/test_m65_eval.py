# -*- coding: utf-8 -*-
"""M6.5 评测跑道自身正确性（外审 #86：跑道不可靠则成绩不可信）。

只测纯函数（summarize / select_contract_files / detect_category），不发网络请求。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_TOOLS = Path(__file__).resolve().parents[1] / "tools" / "m65" / "run_eval.py"
_spec = importlib.util.spec_from_file_location("m65_run_eval", _TOOLS)
assert _spec is not None and _spec.loader is not None  # 路道文件固定存在
m65 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m65)

_F12_TOOLS = Path(__file__).resolve().parents[1] / "tools" / "m65" / "analyze_f12.py"
_f12_spec = importlib.util.spec_from_file_location("m65_analyze_f12", _F12_TOOLS)
assert _f12_spec is not None and _f12_spec.loader is not None
m65f12 = importlib.util.module_from_spec(_f12_spec)
_f12_spec.loader.exec_module(m65f12)


# ---------- summarize：Ask 失败必须进分母（P1：不能只统计交卷的人） ----------

def _fake_results(ask_specs: list[dict]) -> list[dict]:
    """ask_specs: 每项是一次尝试——{'ok': True/False, ...}"""
    return [{"file": "a.docx", "status": "done", "asks": ask_specs,
             "metrics": {}, "report": {"is_docx": True}}]


def test_summarize_failed_asks_stay_in_denominator():
    # 3 次尝试：2 成功 1 失败（异常请求）——失败不能被藏掉
    results = _fake_results([
        {"ok": True, "parse_ok": True, "fields_filled": 4,
         "honest_decline": False, "quote_verified": False},
        {"ok": True, "parse_ok": True, "fields_filled": 4,
         "honest_decline": False, "quote_verified": False},
        {"ok": False, "error": "ReadTimeout"},
    ])
    s = m65.summarize(results, wall_seconds=10.0)
    assert s["ask_attempted"] == 3
    assert s["ask_ok"] == 2
    assert s["ask_failed"] == 1


def test_summarize_parse_and_quality_count_only_successful_asks():
    # parse/字段/诚实度/核验只在成功请求上有意义——失败请求不得混入分子
    results = _fake_results([
        {"ok": True, "parse_ok": True, "fields_filled": 4,
         "honest_decline": False, "quote_verified": True},
        {"ok": False, "error": "HTTPStatusError"},
    ])
    s = m65.summarize(results, wall_seconds=10.0)
    assert s["ask_parse_ok"] == 1  # 失败那次即便误带 parse_ok 也不计入
    assert s["ask_fields_filled_avg"] == 4.0
    assert s["ask_quote_verified"] == 1


def test_summarize_all_failed_asks_no_crash():
    results = _fake_results([{"ok": False, "error": "ConnectError"}])
    s = m65.summarize(results, wall_seconds=1.0)
    assert s["ask_attempted"] == 1
    assert s["ask_ok"] == 0
    assert s["ask_failed"] == 1
    assert s["ask_fields_filled_avg"] is None


def test_summarize_contract_and_report_counts():
    results = [
        {"file": "a", "status": "done", "asks": [], "metrics": {"claim_id_rate": 0.5},
         "report": {"is_docx": True}},
        {"file": "b", "status": "error", "asks": [], "metrics": {},
         "report": {}},
    ]
    s = m65.summarize(results, wall_seconds=2.0)
    assert s["contracts_total"] == 2
    assert s["contracts_done"] == 1
    assert s["report_ok"] == 1
    assert s["claim_id_rates"] == [0.5]


# ---------- select_contract_files：manifest 正式名册 + manifest-extra 附加名册 ----------

@pytest.fixture()
def fixtures_dir(tmp_path: Path) -> Path:
    d = tmp_path / "fixtures"
    d.mkdir()
    for name in ("proc-a.docx", "lease-a.docx", "extra-compilation.pdf"):
        (d / name).write_bytes(b"PK")
    return d


def test_select_files_manifest_is_authority(fixtures_dir: Path):
    manifest = {"proc-a.docx": "procurement", "lease-a.docx": "lease"}
    files, extras = m65.select_contract_files(fixtures_dir, manifest)
    assert [p.name for p in files] == ["lease-a.docx", "proc-a.docx"]
    assert extras == []  # 无附加名册 → 汇编 PDF 绝不混入


def test_select_files_extra_manifest_requires_explicit_category(fixtures_dir: Path):
    manifest = {"proc-a.docx": "procurement", "lease-a.docx": "lease"}
    # 附加名册显式登记品类 → 进 extras
    files, extras = m65.select_contract_files(
        fixtures_dir, manifest, {"extra-compilation.pdf": "procurement"})
    assert [p.name for p in files] == ["lease-a.docx", "proc-a.docx"]
    assert [p.name for p in extras] == ["extra-compilation.pdf"]
    # 附加名册登记了但品类为空 → 拒绝（不猜）
    _, extras2 = m65.select_contract_files(
        fixtures_dir, manifest, {"extra-compilation.pdf": ""})
    assert extras2 == []


def test_select_files_roster_conflict_formal_wins(fixtures_dir: Path):
    # 同一文件同时出现在两个名册 → 正式合同优先，不重复跑
    manifest = {"proc-a.docx": "procurement"}
    extra = {"proc-a.docx": "nda", "extra-compilation.pdf": "procurement"}
    files, extras = m65.select_contract_files(fixtures_dir, manifest, extra)
    assert [p.name for p in files] == ["proc-a.docx"]
    assert [p.name for p in extras] == ["extra-compilation.pdf"]


def test_select_files_manifest_missing_on_disk_reported_not_crashed(fixtures_dir: Path):
    manifest = {"proc-a.docx": "procurement", "ghost.docx": "nda"}
    extra = {"extra-compilation.pdf": "procurement", "lease-a.docx": "lease"}
    files, extras = m65.select_contract_files(fixtures_dir, manifest, extra)
    assert [p.name for p in files] == ["proc-a.docx"]  # 磁盘缺失的 ghost 不进名册
    assert [p.name for p in extras] == ["extra-compilation.pdf", "lease-a.docx"]


# ---------- classify_probe_response：探针三态（P1 外审 #88 终验） ----------

def test_probe_guardrail_refused_regardless_of_error_field():
    # 真实守卫拦截形态：ok=false、无 error、无回答——必须记守卫拒答
    r = m65.classify_probe_response({"ok": False, "error": None}, "a.docx", "i1", "n", 0.0)
    assert r["verdict"] == "guardrail_refused"
    assert r["ok"] is False
    # error 缺字段/空串都不能让它落到 model_*
    r2 = m65.classify_probe_response({"ok": False}, "a.docx", "i1", "n", 0.0)
    assert r2["verdict"] == "guardrail_refused"


def test_probe_model_declined_when_quote_empty_or_unlocatable():
    r = m65.classify_probe_response(
        {"ok": True, "raw_text": '{"原文在哪": "未定位到原文"}'}, "a.docx", "i1", "n", 2.0)
    assert r["verdict"] == "model_declined"
    r2 = m65.classify_probe_response({"ok": True, "raw_text": "{}"}, "a.docx", "i1", "n", 2.0)
    assert r2["verdict"] == "model_declined"


def test_probe_model_fabricated_when_quote_present_without_decline():
    r = m65.classify_probe_response(
        {"ok": True, "raw_text": '{"原文在哪": "第五条 乙方应于每月五日前支付租金。"}'},
        "a.docx", "i1", "n", 2.0)
    assert r["verdict"] == "model_fabricated"


# ---------- detect_category：manifest 优先于文件名猜测 ----------

def test_detect_category_manifest_wins_over_filename_hint():
    # 文件名含「采购」但 manifest 明确是 nda——manifest 说了算
    assert m65.detect_category("采购合同.docx", {"采购合同.docx": "nda"}) == "nda"


# ---------- require_tls：Basic 凭据不许默认走明文公网（P1 外审 #86） ----------

def test_require_tls_allows_https_and_loopback_only():
    assert m65.require_tls("https://example.com") is True
    assert m65.require_tls("http://localhost:8080") is True
    assert m65.require_tls("http://127.0.0.1:8080") is True
    # 生产 HTTP 公网地址默认拒绝——须显式 --insecure
    assert m65.require_tls("http://59.110.13.13:8080") is False


# ---------- analyze_f2：锚点未匹配必须挂起，绝不全文扫描（P2 外审 #88） ----------

def _f2_rec(quote: str) -> dict:
    return {"file": "x.docx", "items_detail": [
        {"name": "违约责任", "status": "需关注", "note": "单方免除乙方违约责任",
         "quote": quote}]}


def test_analyze_f2_anchor_miss_pends_even_if_text_has_equality_words():
    text = "第一条 双方应诚信履约。第八条 乙方违约的，乙方向甲方支付违约金。"
    # 引句与原文空白/换行被 API 吃掉导致匹配不上——不许退回全文找「双方」
    rows = m65f12.analyze_f2(_f2_rec("…第七条 甲方应在签约后十个工作日内交付定金。"), text)
    assert len(rows) == 1
    assert rows[0]["verdict"].startswith("待人工复核")
    assert rows[0]["anchor_matched"] is False  # 锚点没匹配上——不许借全文找「双方」
    assert rows[0]["signal_hits"] == []


def test_analyze_f2_anchor_match_detects_equal_language_nearby():
    text = "第九条 任何一方对由于不可抗力造成的不能履行合同不承担违约责任，双方各自承担风险。"
    quote = "…任何一方对由于不可抗力造成的不能履行合同不承担违约责任。"
    rows = m65f12.analyze_f2(_f2_rec(quote), text)
    assert rows[0]["anchor_matched"] is True
    assert rows[0]["verdict"] == "误报嫌疑（原文对等）"
    assert "任何一方" in rows[0]["signal_hits"]


def test_detect_category_fallback_hints_and_default():
    assert m65.detect_category("设备租赁.docx", {}) == "lease"
    assert m65.detect_category("trade-secret-nda-x.docx", {}) == "nda"
    assert m65.detect_category("unknown-name.docx", {}) == "procurement"
