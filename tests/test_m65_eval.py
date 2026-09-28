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


# ---------- select_contract_files：manifest 是唯一名册（P1：汇编 PDF 不得混入） ----------

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
    assert extras == []  # 汇编 PDF 默认绝不混入合同统计


def test_select_files_include_all_routes_extras_separately(fixtures_dir: Path):
    manifest = {"proc-a.docx": "procurement", "lease-a.docx": "lease"}
    files, extras = m65.select_contract_files(fixtures_dir, manifest, include_all=True)
    assert [p.name for p in files] == ["lease-a.docx", "proc-a.docx"]
    assert [p.name for p in extras] == ["extra-compilation.pdf"]


def test_select_files_manifest_missing_on_disk_reported_not_crashed(fixtures_dir: Path):
    manifest = {"proc-a.docx": "procurement", "ghost.docx": "nda"}
    files, _ = m65.select_contract_files(fixtures_dir, manifest)
    assert [p.name for p in files] == ["proc-a.docx"]  # 磁盘缺失的 ghost 不进名册
    # 名册外的磁盘文件（含名册漏登记的真实合同）只在 --all 下进 extras
    _, extras = m65.select_contract_files(fixtures_dir, manifest, include_all=True)
    assert [p.name for p in extras] == ["extra-compilation.pdf", "lease-a.docx"]


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


def test_detect_category_fallback_hints_and_default():
    assert m65.detect_category("设备租赁.docx", {}) == "lease"
    assert m65.detect_category("trade-secret-nda-x.docx", {}) == "nda"
    assert m65.detect_category("unknown-name.docx", {}) == "procurement"
