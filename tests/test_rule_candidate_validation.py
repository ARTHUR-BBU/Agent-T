"""Candidate checks must execute against candidate bytes, not current disk rules."""
import hashlib
import os
import subprocess
import time

import threading

import pytest

from app.services import rule_candidate_validation
from app.services.rule_candidate_builder import build_candidate
from app.services.rule_candidate_validation import current_input_manifest, run_gold_checks
from app.services.rule_snapshot import capture_rule_snapshot


@pytest.fixture(autouse=True)
def _fresh_process_control_gate(monkeypatch):
    """测试隔离（2026-11-13 全量回归暴露）：_PROCESS_CONTROL_UNCERTAIN 是
    模块级 Event——同进程里任何更早测试的金标清理失败/超时都会置位并
    粘住，后续金标测试全部被「上次清理状态未知」拒绝（跨测试干扰：
    全量跑批 3 failed 复现；本文件单独运行 9/9 通过）。生产粘住语义不动：
    服务进程内真实事故仍全程拦截；这里只给测试进程每例换新 Event。"""
    event = threading.Event()
    monkeypatch.setattr(rule_candidate_validation,
                        "_PROCESS_CONTROL_UNCERTAIN", event)
    yield
    event.set()  # 测试内故意置位的用例结束后不再泄漏给其他文件


def test_gold_runner_checks_candidate_in_isolated_process():
    baseline = capture_rule_snapshot("procurement")
    valid = run_gold_checks(baseline, timeout_seconds=60)
    assert valid.passed is True and valid.exit_code == 0 and valid.passed_tests >= 1
    assert valid.input_manifest_sha256 == current_input_manifest(baseline)
    draft = {"category": "procurement", "operation": "disable", "item_id": "subject_matter",
             "reason": "验证候选规则", "base_sha256": hashlib.sha256(baseline.config_bytes).hexdigest()}
    candidate = build_candidate(baseline, draft).snapshot
    invalid = run_gold_checks(candidate, timeout_seconds=60)
    assert invalid.passed is False and invalid.exit_code != 0
    assert any("test_procurement_gold_attention_items" in item for item in invalid.failed_test_ids)
    assert invalid.candidate_sha256 == hashlib.sha256(candidate.config_bytes).hexdigest()
    assert invalid.candidate_sha256 != valid.candidate_sha256
    original = next(i for i in baseline.configuration()["items"] if i["id"] == "subject_matter")
    edit = {**draft, "operation": "edit", "name": original["name"] + "（编辑）", "description": original["description"]}
    renamed = build_candidate(baseline, edit).snapshot
    assert run_gold_checks(renamed, timeout_seconds=90).passed is True


def test_manifest_changes_when_scoring_config_changes(tmp_path, monkeypatch):
    from app.services import rule_candidate_validation as validator

    for name in ("app/example.py", "config/scorecard_forbidden.yaml", "tests/gold.py",
                 "tests/conftest.py", "tests/helpers.py", "fixtures/example.txt", "pyproject.toml"):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("original", encoding="utf-8")
    monkeypatch.setattr(validator, "_ROOT", tmp_path)
    before = validator._manifest("tests/gold.py", ("fixtures/example.txt",))
    (tmp_path / "config/scorecard_forbidden.yaml").write_text("changed", encoding="utf-8")
    assert validator._manifest("tests/gold.py", ("fixtures/example.txt",)) != before
    before = validator._manifest("tests/gold.py", ("fixtures/example.txt",))
    (tmp_path / "pytest.ini").write_text("[pytest]\naddopts = -k does_not_exist\n", encoding="utf-8")
    assert validator._manifest("tests/gold.py", ("fixtures/example.txt",)) != before


def test_nda_candidate_may_add_item_without_weakening_gold_risk_checks():
    baseline = capture_rule_snapshot("nda")
    draft = {"category": "nda", "operation": "add", "item_id": "custom_notice",
             "name": "通知", "description": "检查通知", "reason": "核查通知", "segment": "B",
             "literal": "通知方式", "positive": "通知方式为书面", "negative": "书面送达",
             "base_sha256": hashlib.sha256(baseline.config_bytes).hexdigest()}
    candidate = build_candidate(baseline, draft).snapshot
    result = run_gold_checks(candidate, timeout_seconds=120)
    assert result.passed is True and result.passed_tests == result.collected_tests


def test_skipped_gold_test_is_not_reported_as_passed(tmp_path, monkeypatch):
    from app.services import rule_candidate_validation as validator

    candidate = capture_rule_snapshot("procurement")
    files = {
        "app/__init__.py": "",
        "app/services/__init__.py": "",
        "app/services/checklist.py": "CONFIG_DIR = None\n",
        "config/example.yaml": "value: 1\n",
        "tests/conftest.py": "",
        "tests/helpers.py": "",
        "tests/gold.py": "import pytest\ndef test_ok(): assert True\n@pytest.mark.skip(reason='not run')\ndef test_missing(): pass\n",
        "fixtures/example.txt": "synthetic",
        "pyproject.toml": "[tool.pytest.ini_options]\n",
    }
    for name, content in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    monkeypatch.setattr(validator, "_ROOT", tmp_path)
    monkeypatch.setitem(validator._SUITES, "procurement", ("tests/gold.py", ("fixtures/example.txt",)))
    result = validator.run_gold_checks(candidate, timeout_seconds=90)
    assert result.exit_code == 0
    assert result.collected_tests == 2 and result.passed_tests == 1 and result.skipped_tests == 1
    assert result.passed is False


def test_timeout_is_not_reported_as_passed(tmp_path, monkeypatch):
    from app.services import rule_candidate_validation as validator
    from app.services.regression_process import ProcessTreeResult

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="pytest", timeout=1)

    if os.name == "nt":
        work = tmp_path / "timed-out"
        work.mkdir()
        monkeypatch.setattr(validator.tempfile, "mkdtemp", lambda **kwargs: str(work))
        monkeypatch.setattr(validator, "run_process_tree", lambda *args, **kwargs: ProcessTreeResult(None, True, False))
    else:
        monkeypatch.setattr(validator.subprocess, "run", timeout)
    validator._PROCESS_CONTROL_UNCERTAIN.clear()
    try:
        result = validator.run_gold_checks(capture_rule_snapshot("procurement"), timeout_seconds=1)
        assert result.passed is False and result.exit_code is None and result.passed_tests == 0
        if os.name == "nt":
            assert (work / "config").is_dir()
            with pytest.raises(RuntimeError, match="清理状态未知"):
                validator.run_gold_checks(capture_rule_snapshot("procurement"))
    finally:
        validator._PROCESS_CONTROL_UNCERTAIN.clear()


@pytest.mark.skipif(os.name != "nt", reason="Windows 进程控制专项")
def test_interrupted_descendant_is_not_reported_as_passed(tmp_path, monkeypatch):
    from app.services import rule_candidate_validation as validator
    from app.services.regression_process import ProcessTreeResult

    work = tmp_path / "interrupted"
    work.mkdir()
    monkeypatch.setattr(validator.tempfile, "mkdtemp", lambda **kwargs: str(work))
    monkeypatch.setattr(validator, "run_process_tree", lambda *args, **kwargs: ProcessTreeResult(0, False, True))
    validator._PROCESS_CONTROL_UNCERTAIN.clear()
    try:
        result = validator.run_gold_checks(capture_rule_snapshot("procurement"))
        assert not result.passed and result.exit_code is None
        assert result.process_control == "windows_job" and result.output_sha256 is None and result.output_capture == "discarded"
        assert (work / "config").is_dir()
        with pytest.raises(RuntimeError, match="清理状态未知"):
            validator.run_gold_checks(capture_rule_snapshot("procurement"))
    finally:
        validator._PROCESS_CONTROL_UNCERTAIN.clear()


@pytest.mark.skipif(os.name != "nt", reason="Windows 进程控制专项")
def test_control_failure_preserves_directory_and_blocks_next_run(tmp_path, monkeypatch):
    from app.services import rule_candidate_validation as validator

    work = tmp_path / "retained"
    work.mkdir()
    monkeypatch.setattr(validator.tempfile, "mkdtemp", lambda **kwargs: str(work))

    def fail(*args, **kwargs):
        raise RuntimeError("synthetic control failure")

    monkeypatch.setattr(validator, "run_process_tree", fail)
    validator._PROCESS_CONTROL_UNCERTAIN.clear()
    try:
        with pytest.raises(RuntimeError, match="synthetic control failure"):
            validator.run_gold_checks(capture_rule_snapshot("procurement"))
        assert (work / "config").is_dir()
        with pytest.raises(RuntimeError, match="清理状态未知"):
            validator.run_gold_checks(capture_rule_snapshot("procurement"))
    finally:
        validator._PROCESS_CONTROL_UNCERTAIN.clear()


def test_structured_result_rejects_malformed_or_oversized_input(tmp_path):
    from app.services import rule_candidate_validation as validator

    result = tmp_path / "result.json"
    result.write_text('{"collected": true, "outcomes": {}}', encoding="utf-8")
    assert validator._read_structured_result(result) == (0, [], ())
    result.write_bytes(b"x" * (validator._MAX_RESULT_BYTES + 1))
    assert validator._read_structured_result(result) == (0, [], ())


@pytest.mark.skipif(os.name != "nt", reason="Windows 进程控制专项")
def test_actual_gold_timeout_stops_child_and_preserves_work(tmp_path, monkeypatch):
    from app.services import rule_candidate_validation as validator

    candidate = capture_rule_snapshot("procurement")
    root = tmp_path / "source"
    marker, ready, release = tmp_path / "late.txt", tmp_path / "ready.txt", tmp_path / "release.txt"
    child_script = (
        "import os,pathlib,time\n"
        f"pathlib.Path({str(ready)!r}).write_text(str(os.getpid()))\n"
        "deadline = time.monotonic() + 45\n"
        f"while not pathlib.Path({str(release)!r}).exists() and time.monotonic() < deadline: time.sleep(.05)\n"
        f"if pathlib.Path({str(release)!r}).exists(): pathlib.Path({str(marker)!r}).write_text('late')\n"
    )
    files = {
        "app/__init__.py": "", "app/services/__init__.py": "", "app/services/checklist.py": "CONFIG_DIR = None\n",
        "config/example.yaml": "value: 1\n", "tests/conftest.py": "", "tests/helpers.py": "",
        "fixtures/example.txt": "synthetic", "pyproject.toml": "[tool.pytest.ini_options]\n",
        "tests/gold.py": (
            "import pathlib, subprocess, sys, time\n"
            "def test_hangs():\n"
            f"    child = {child_script!r}\n"
            "    subprocess.Popen([sys.executable, '-c', child])\n"
            "    time.sleep(30)\n"
        ),
    }
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    work = tmp_path / "retained"
    work.mkdir()
    monkeypatch.setattr(validator, "_ROOT", root)
    monkeypatch.setitem(validator._SUITES, "procurement", ("tests/gold.py", ("fixtures/example.txt",)))
    monkeypatch.setattr(validator.tempfile, "mkdtemp", lambda **kwargs: str(work))
    validator._PROCESS_CONTROL_UNCERTAIN.clear()
    try:
        result = validator.run_gold_checks(candidate, timeout_seconds=20)
        assert ready.exists(), "synthetic child must actually start before timeout"
        assert not result.passed and result.exit_code is None
        assert (work / "config").is_dir()
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel.CloseHandle.restype = wintypes.BOOL
        handle = kernel.OpenProcess(0x00100000, False, int(ready.read_text(encoding="utf-8")))
        if handle:
            try:
                assert kernel.WaitForSingleObject(handle, 0) == 0, "synthetic descendant is still running"
            finally:
                assert kernel.CloseHandle(handle)
        else:
            assert ctypes.get_last_error() == 87, "cannot verify descendant termination"
        with pytest.raises(RuntimeError, match="清理状态未知"):
            validator.run_gold_checks(candidate)
        release.write_text("release", encoding="utf-8")
        time.sleep(3)
        assert not marker.exists()
    finally:
        release.write_text("release", encoding="utf-8")
        validator._PROCESS_CONTROL_UNCERTAIN.clear()
