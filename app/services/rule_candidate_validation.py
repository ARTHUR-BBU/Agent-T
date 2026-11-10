"""Local gold-test runner for candidate bytes; not a production release gate.

Runs the existing category gold suite in a separate Python process with a
temporary configuration directory and no model credentials. Returned results
are not persisted approvals and do not assert legal or full regression coverage.
"""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import site
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from app.services import checklist
from app.services.rule_snapshot import RuleSnapshot
from app.services.regression_process import run_process_tree

_ROOT = Path(__file__).resolve().parents[2]
_PROCESS_CONTROL_UNCERTAIN = threading.Event()
_MAX_RESULT_BYTES = 1024 * 1024


def process_control_uncertain() -> bool:
    return _PROCESS_CONTROL_UNCERTAIN.is_set()
_SUITES = {
    "procurement": ("tests/test_procurement_gold.py", ("fixtures/procurement_sample.txt",)),
    "lease": ("tests/test_lease_gold.py", ("fixtures/lease_deposit_late.txt", "fixtures/lease_early_term.txt",
                                           "fixtures/lease_four_risk.txt", "fixtures/lease_maintenance.txt",
                                           "fixtures/lease_sample.txt")),
    "nda": ("tests/test_nda_gold.py", ("fixtures/nda_adversarial.txt", "fixtures/nda_gold_risks.txt",
                                       "fixtures/nda_public_template.txt")),
    "technical_service": ("tests/test_tisvc_gold.py", tuple(
        f"fixtures-tisvc/{name}.txt" for name in ("techdev-coop-most-2001", "techdev-most-2001",
                                                  "tisvc-most-2001", "tisvc-shgov-2020", "tisvc-sjtu-2003")
    )),
}


@dataclass(frozen=True)
class GoldCheckResult:
    category: str
    candidate_sha256: str
    input_manifest_sha256: str
    suite: str
    passed: bool
    passed_tests: int
    collected_tests: int
    skipped_tests: int
    failed_test_ids: tuple[str, ...]
    exit_code: int | None
    output_sha256: str | None
    scope: str = "品类金标专项，使用当前环境依赖；不包含完整回归、沙箱隔离、发布审批或法律适用性"
    process_control: str = "legacy_subprocess"
    output_capture: str = "stdout_sha256"


@contextmanager
def _gold_temporary_directory():
    """Preserve files if process control failed and cleanup safety is unknown."""
    path = Path(tempfile.mkdtemp(prefix="agent-t-rule-check-"))
    state = {"cleanup_safe": True}
    try:
        yield path, state
    finally:
        if state["cleanup_safe"]:
            try:
                shutil.rmtree(path)
            except OSError as exc:
                if os.name == "nt":
                    _PROCESS_CONTROL_UNCERTAIN.set()
                raise RuntimeError("测试工作区清理失败，请停止服务并检查") from exc


def _read_structured_result(path: Path) -> tuple[int, list[str], tuple[str, ...]]:
    try:
        with path.open("rb") as stream:
            raw = stream.read(_MAX_RESULT_BYTES + 1)
        if len(raw) > _MAX_RESULT_BYTES:
            raise ValueError("测试结果过大")
        structured = json.loads(raw.decode("utf-8"))
        collected, reports = structured["collected"], structured["outcomes"]
        if (type(collected) is not int or collected < 0 or not isinstance(reports, dict)
                or len(reports) > collected
                or any(not isinstance(key, str) or len(key) > 2048 or value not in {"passed", "failed", "skipped", "xfail"}
                       for key, value in reports.items())):
            raise ValueError("测试结果结构无效")
        failed_ids = tuple(key[:256] for key, outcome in reports.items() if outcome == "failed")[:20]
        return collected, list(reports.values()), failed_ids
    except (OSError, ValueError, TypeError, KeyError, AttributeError, UnicodeDecodeError):
        return 0, [], ()


def _manifest(suite: str, fixtures: tuple[str, ...]) -> str:
    files = sorted((_ROOT / "app").rglob("*.py"))
    files += sorted((_ROOT / "config").rglob("*.yaml"))
    files += [_ROOT / "pyproject.toml"]
    for name in ("pytest.ini", ".pytest.ini", "tox.ini", "setup.cfg", "requirements.txt",
                 "requirements-dev.txt", "requirements-lock.txt", "requirements-hf.txt"):
        path = _ROOT / name
        if path.is_file() and not path.is_symlink():
            files.append(path)
    files += [_ROOT / suite, _ROOT / "tests/conftest.py", _ROOT / "tests/helpers.py"]
    files += [_ROOT / fixture for fixture in fixtures]
    digest = hashlib.sha256()
    environment = {"python": sys.version,
                   "packages": sorted((dist.metadata["Name"] or "", dist.version)
                                      for dist in importlib.metadata.distributions())}
    digest.update(json.dumps(environment, ensure_ascii=True, sort_keys=True).encode("utf-8"))
    for path in sorted(files):
        name = path.relative_to(_ROOT).as_posix().encode("utf-8")
        content = path.read_bytes()
        digest.update(len(name).to_bytes(8, "big") + name)
        digest.update(len(content).to_bytes(8, "big") + content)
    return digest.hexdigest()


def _config_entries_manifest(entries: dict[str, bytes]) -> str:
    digest = hashlib.sha256()
    for name_text, content in sorted(entries.items()):
        name = name_text.encode("utf-8")
        digest.update(len(name).to_bytes(8, "big") + name)
        digest.update(len(content).to_bytes(8, "big") + content)
    return digest.hexdigest()


def _config_manifest(config_dir: Path) -> str:
    return _config_entries_manifest({path.name: path.read_bytes() for path in config_dir.glob("*.yaml")})


def current_input_manifest(candidate: RuleSnapshot) -> str:
    if candidate.category not in _SUITES:
        raise ValueError("该品类没有配置金标测试")
    suite, fixtures = _SUITES[candidate.category]
    entries = {path.name: path.read_bytes() for path in checklist.CONFIG_DIR.glob("checklist_*.yaml")}
    entries[f"checklist_{candidate.category}.yaml"] = candidate.config_bytes
    return hashlib.sha256((_manifest(suite, fixtures) + _config_entries_manifest(entries)).encode("ascii")).hexdigest()


def run_gold_checks(candidate: RuleSnapshot, *, timeout_seconds: int = 120) -> GoldCheckResult:
    if _PROCESS_CONTROL_UNCERTAIN.is_set():
        raise RuntimeError("上次测试进程清理状态未知，请停止服务并检查")
    if candidate.category not in _SUITES:
        raise ValueError("该品类没有配置金标测试")
    if not isinstance(timeout_seconds, int) or isinstance(timeout_seconds, bool) or not 1 <= timeout_seconds <= 300:
        raise ValueError("测试时限无效")
    suite, fixtures = _SUITES[candidate.category]
    candidate_digest = hashlib.sha256(candidate.config_bytes).hexdigest()
    before = _manifest(suite, fixtures)
    with _gold_temporary_directory() as (temp_path, temp_state):
        config_dir = temp_path / "config"
        config_dir.mkdir()
        for path in checklist.CONFIG_DIR.glob("checklist_*.yaml"):
            shutil.copyfile(path, config_dir / path.name)
        (config_dir / f"checklist_{candidate.category}.yaml").write_bytes(candidate.config_bytes)
        initial_config = _config_manifest(config_dir)
        env = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP") if key in os.environ}
        package_paths = {sysconfig.get_path("purelib"), sysconfig.get_path("platlib"), site.getusersitepackages()}
        import_paths = [str(_ROOT)] + sorted(str(path) for path in package_paths if path and Path(path).is_dir())
        env.update(PYTHONPATH=os.pathsep.join(import_paths), PYTHONUTF8="1", PYTHONNOUSERSITE="1",
                   AGENT_RULE_CANDIDATE_CHECK="1",
                   PYTHONPYCACHEPREFIX=str(temp_path / "pycache"),
                   PYTEST_DISABLE_PLUGIN_AUTOLOAD="1", STORE_DB_PATH=str(temp_path / "reviews.sqlite"),
                   TEMP=str(temp_path), TMP=str(temp_path))
        result_file = temp_path / "pytest-result.json"
        script = """
import json, sys
from pathlib import Path
from app.services import checklist
checklist.CONFIG_DIR = Path(sys.argv[1])
import pytest
class Recorder:
    def __init__(self):
        self.outcomes = {}
    def pytest_runtest_logreport(self, report):
        if report.when == 'call' or report.failed or report.skipped:
            self.outcomes[report.nodeid] = 'xfail' if hasattr(report, 'wasxfail') else report.outcome
    def pytest_sessionfinish(self, session, exitstatus):
        Path(sys.argv[3]).write_text(json.dumps({'collected': session.testscollected, 'outcomes': self.outcomes}), encoding='utf-8')
sys.exit(pytest.main([sys.argv[2], '-q', '-p', 'no:cacheprovider'], plugins=[Recorder()]))
"""
        command = [sys.executable, "-c", script, str(config_dir), str(_ROOT / suite), str(result_file)]
        if os.name == "nt":
            temp_state["cleanup_safe"] = False
            try:
                controlled = run_process_tree(command, cwd=_ROOT, env=env, timeout_seconds=timeout_seconds)
            except BaseException:
                _PROCESS_CONTROL_UNCERTAIN.set()
                raise
            if controlled.timed_out or controlled.interrupted_descendants:
                _PROCESS_CONTROL_UNCERTAIN.set()
            else:
                temp_state["cleanup_safe"] = True
            output_sha256, output_capture, process_control = None, "discarded", "windows_job"
            exit_code = None if controlled.timed_out or controlled.interrupted_descendants else controlled.exit_code
        else:
            try:
                proc = subprocess.run(command, cwd=_ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                      timeout=timeout_seconds, check=False)
                output, exit_code = proc.stdout, proc.returncode
            except subprocess.TimeoutExpired as exc:
                output, exit_code = exc.stdout or b"", None
            output_sha256, output_capture, process_control = hashlib.sha256(output).hexdigest(), "stdout_sha256", "legacy_subprocess"
        collected, outcomes, failed_ids = _read_structured_result(result_file)
        final_config = _config_manifest(config_dir)
    after = _manifest(suite, fixtures)
    passed_tests = outcomes.count("passed")
    skipped_tests = outcomes.count("skipped") + outcomes.count("xfail")
    return GoldCheckResult(category=candidate.category, candidate_sha256=candidate_digest,
                           input_manifest_sha256=hashlib.sha256((before + initial_config).encode("ascii")).hexdigest(), suite=suite,
                           passed=(exit_code == 0 and collected > 0 and passed_tests == collected
                                   and before == after and initial_config == final_config),
                           passed_tests=passed_tests, collected_tests=collected, skipped_tests=skipped_tests,
                           failed_test_ids=failed_ids, exit_code=exit_code,
                           output_sha256=output_sha256, output_capture=output_capture, process_control=process_control)
