"""Candidate inputs are copied; child processes actually load the substituted rules."""
import hashlib
import json
import os
import site
import subprocess
import sys
import sysconfig
import time
import urllib.error
import urllib.parse
import urllib.request

import pytest

from app.services.rule_candidate_builder import build_candidate
from app.services.rule_snapshot import capture_rule_snapshot
from app.services.candidate_workspace import prepare_candidate_workspace


def test_isolated_child_process_reads_candidate_and_source_stays_unchanged():
    baseline = capture_rule_snapshot("procurement")
    candidate = build_candidate(baseline, {
        "category": "procurement", "operation": "disable", "item_id": "subject_matter",
        "reason": "隔离测试", "base_sha256": baseline.content_sha256,
    }).snapshot
    source = baseline.config_bytes
    with prepare_candidate_workspace(candidate) as workspace:
        copy_root = workspace.root
        assert not (copy_root / ".env").exists()
        assert (copy_root / "config" / "checklist_procurement.yaml").read_bytes() == candidate.config_bytes
        env = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP") if key in os.environ}
        package_paths = {sysconfig.get_path("purelib"), sysconfig.get_path("platlib"), site.getusersitepackages()}
        import_paths = [str(copy_root)] + sorted(str(path) for path in package_paths if path and os.path.isdir(path))
        env.update(PYTHONPATH=os.pathsep.join(import_paths), PYTHONUTF8="1", PYTHONNOUSERSITE="1",
                   PYTEST_DISABLE_PLUGIN_AUTOLOAD="1", STORE_DB_PATH=str(copy_root / "test-review.sqlite"))
        script = """
import json
from pathlib import Path
from app.services import checklist
from app.services.rule_snapshot import capture_rule_snapshot
snapshot = capture_rule_snapshot('procurement')
result = checklist.run_checklist('虚构采购合同', 'procurement')
print(json.dumps({'module': str(Path(checklist.__file__).resolve()),
                  'config': str(checklist.CONFIG_DIR.resolve()),
                  'sha256': snapshot.content_sha256,
                  'ids': [item['id'] for item in result['items']]}))
"""
        result = subprocess.run([sys.executable, "-c", script], cwd=copy_root, env=env, capture_output=True,
                                text=True, encoding="utf-8", timeout=60, check=True)
        info = json.loads(result.stdout)
        assert info["sha256"] == candidate.content_sha256
        assert info["module"].startswith(str(copy_root)) and info["config"] == str(copy_root / "config")
        assert "subject_matter" not in info["ids"]
        target = copy_root / "config" / "checklist_procurement.yaml"
        command = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                   "tests/test_procurement_gold.py::test_procurement_gold_attention_items"]
        target.write_bytes(source)
        good = subprocess.run(command, cwd=copy_root, env=env, capture_output=True, text=True, encoding="utf-8", timeout=60)
        assert good.returncode == 0 and "1 passed" in good.stdout
        target.write_bytes(candidate.config_bytes)
        control = subprocess.run(command, cwd=copy_root, env=env, capture_output=True, text=True, encoding="utf-8", timeout=60)
        assert control.returncode == 1
        assert "checklist missing items" in control.stdout and "subject_matter" in control.stdout and "1 failed" in control.stdout
        assert capture_rule_snapshot("procurement").config_bytes == source
    assert not copy_root.exists()


def test_source_drift_during_copy_aborts(tmp_path, monkeypatch):
    from app.services import candidate_workspace

    snapshot = capture_rule_snapshot("procurement")
    for folder in ("app", "config", "tests"):
        (tmp_path / folder).mkdir()
    (tmp_path / "app" / "example.py").write_text("original", encoding="utf-8")
    (tmp_path / "config" / "checklist_procurement.yaml").write_bytes(snapshot.config_bytes)
    monkeypatch.setattr(candidate_workspace, "_ROOT", tmp_path)
    real_manifest = candidate_workspace._full_manifest_at
    changed = []

    def change_after_hash(*args, **kwargs):
        result = real_manifest(*args, **kwargs)
        if not changed:
            (tmp_path / "app" / "example.py").write_text("changed", encoding="utf-8")
            changed.append(True)
        return result

    monkeypatch.setattr(candidate_workspace, "_full_manifest_at", change_after_hash)
    with pytest.raises(RuntimeError, match="复制期间发生变化"):
        with prepare_candidate_workspace(snapshot):
            pytest.fail("changed input must not be used")
    assert hashlib.sha256((tmp_path / "config" / "checklist_procurement.yaml").read_bytes()).hexdigest() == snapshot.content_sha256


def test_workspace_rejects_input_over_size_limit_before_copy(tmp_path, monkeypatch):
    from app.services import candidate_workspace

    snapshot = capture_rule_snapshot("procurement")
    for folder in ("app", "config", "tests"):
        (tmp_path / folder).mkdir()
    (tmp_path / "config" / "checklist_procurement.yaml").write_bytes(snapshot.config_bytes)
    monkeypatch.setattr(candidate_workspace, "_ROOT", tmp_path)
    monkeypatch.setattr(candidate_workspace, "_MAX_BYTES", 1)
    with pytest.raises(RuntimeError, match="大小超限"):
        with prepare_candidate_workspace(snapshot):
            pytest.fail("oversized input must not be used")


def test_candidate_digest_rejects_growth_beyond_limit(tmp_path, monkeypatch):
    from app.services import candidate_workspace

    path = tmp_path / "candidate.yaml"
    path.write_bytes(b"too large")
    monkeypatch.setattr(candidate_workspace, "_MAX_BYTES", 1)
    with pytest.raises(RuntimeError, match="大小超限"):
        candidate_workspace._bounded_digest(path)


def test_workspace_rejects_changes_during_execution(tmp_path, monkeypatch):
    from app.services import candidate_workspace

    snapshot = capture_rule_snapshot("procurement")
    for folder in ("app", "config", "tests"):
        (tmp_path / folder).mkdir()
    (tmp_path / "config" / "checklist_procurement.yaml").write_bytes(snapshot.config_bytes)
    monkeypatch.setattr(candidate_workspace, "_ROOT", tmp_path)
    with pytest.raises(RuntimeError, match="执行期间发生变化"):
        with prepare_candidate_workspace(snapshot) as workspace:
            (workspace.root / "tests").mkdir(exist_ok=True)
            (workspace.root / "tests" / "injected.py").write_text("changed", encoding="utf-8")


def test_workspace_rejects_source_changes_during_execution(tmp_path, monkeypatch):
    from app.services import candidate_workspace

    snapshot = capture_rule_snapshot("procurement")
    for folder in ("app", "config", "tests"):
        (tmp_path / folder).mkdir()
    (tmp_path / "config" / "checklist_procurement.yaml").write_bytes(snapshot.config_bytes)
    monkeypatch.setattr(candidate_workspace, "_ROOT", tmp_path)
    with pytest.raises(RuntimeError, match="执行期间发生变化"):
        with prepare_candidate_workspace(snapshot):
            (tmp_path / "tests" / "new_test.py").write_text("changed", encoding="utf-8")


def test_candidate_is_used_by_real_http_review_process():
    baseline = capture_rule_snapshot("procurement")
    candidate = build_candidate(baseline, {
        "category": "procurement", "operation": "disable", "item_id": "subject_matter",
        "reason": "验证独立服务", "base_sha256": baseline.content_sha256,
    }).snapshot
    with prepare_candidate_workspace(candidate) as workspace:
        root = workspace.root
        target = root / "config" / "checklist_procurement.yaml"
        package_paths = {sysconfig.get_path("purelib"), sysconfig.get_path("platlib"), site.getusersitepackages()}
        import_paths = [str(root)] + sorted(str(path) for path in package_paths if path and os.path.isdir(path))

        def run_review(snapshot, name):
            target.write_bytes(snapshot.config_bytes)
            runtime = root / f"runtime-{name}"
            runtime.mkdir()
            port_file = runtime / "port.txt"
            env = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP") if key in os.environ}
            env.update(PYTHONPATH=os.pathsep.join(import_paths), PYTHONUTF8="1", PYTHONNOUSERSITE="1",
                       STORE_DB_PATH=str(root / f"review-{name}.sqlite"), OWNED_LLM_EXECUTION="0",
                       PRECHECK_ENABLED="false", CATEGORY_SUGGEST_ENABLED="false", QUALITY_ENABLED="false",
                       OBJECTIONS_ENABLED="false", RATE_LIMIT_UPLOAD_PER_MINUTE="0", TEMP=str(runtime), TMP=str(runtime),
                       TEST_PORT_FILE=str(port_file))
            script = """
import os, socket
from pathlib import Path
import uvicorn
sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
sock.bind(('127.0.0.1', 0))
sock.listen(128)
port_file = Path(os.environ['TEST_PORT_FILE'])
temporary = port_file.with_suffix('.tmp')
temporary.write_text(str(sock.getsockname()[1]), encoding='ascii')
os.replace(temporary, port_file)
server = uvicorn.Server(uvicorn.Config('app.main:app', log_level='warning'))
server.run(sockets=[sock])
"""
            proc = subprocess.Popen([sys.executable, "-c", script], cwd=root, env=env,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                deadline = time.monotonic() + 45
                while True:
                    if proc.poll() is not None:
                        pytest.fail(f"isolated server exited: {proc.returncode}")
                    if not port_file.exists():
                        if time.monotonic() >= deadline:
                            pytest.fail("isolated server did not bind")
                        time.sleep(.1)
                        continue
                    url = f"http://127.0.0.1:{int(port_file.read_text(encoding='ascii'))}"
                    try:
                        with urllib.request.urlopen(url + "/health", timeout=1) as response:
                            assert response.status == 200
                        break
                    except (OSError, urllib.error.URLError):
                        if time.monotonic() >= deadline:
                            pytest.fail("isolated server did not start")
                        time.sleep(.1)
                payload = urllib.parse.urlencode({"pasted_text": "虚构采购合同：甲方向乙方购买设备，价格一万元。",
                                                  "category": "procurement", "stance": "neutral"}).encode()
                request = urllib.request.Request(url + "/api/upload", data=payload, method="POST")
                with urllib.request.urlopen(request, timeout=30) as response:
                    review_id = json.load(response)["review_id"]
                deadline = time.monotonic() + 45
                while True:
                    with urllib.request.urlopen(url + "/api/review/" + review_id, timeout=5) as response:
                        data = json.load(response)
                    if data["status"] in ("done", "error"):
                        break
                    if time.monotonic() >= deadline:
                        pytest.fail("isolated review did not finish")
                    time.sleep(.1)
                assert data["status"] == "done"
                assert data["rule_provenance"] == {"source": "local_file", "category": "procurement",
                                                   "content_sha256": snapshot.content_sha256}
                return {item["id"] for item in data["items"]}
            finally:
                if proc.poll() is None:
                    proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=10)

        assert "subject_matter" in run_review(baseline, "baseline")
        assert "subject_matter" not in run_review(candidate, "candidate")
        assert capture_rule_snapshot("procurement").config_bytes == baseline.config_bytes
