"""Real pipeline in an isolated account store with synthetic contracts and fake HTTP."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from app.services import owned_llm_transport
from app.services.account_store import AuthenticationError, AuthorizationError
from app.services.owned_llm_ledger import OwnedLLMLedger
from app.services.owned_review_worker import OwnedReviewWorker
from app.services.rule_snapshot import capture_rule_snapshot
from app.services.rule_versions import RuleVersionStore
from tests.test_owned_review_jobs import jobs  # noqa: F401


@pytest.fixture
def worker(jobs, monkeypatch):  # noqa: F811
    accounts, owned, service, admin, alice_user, alice, bob = jobs
    RuleVersionStore(accounts).initialize(admin, "procurement", capture_rule_snapshot("procurement").config_bytes)
    ledger = OwnedLLMLedger(service)
    monkeypatch.setattr(owned_llm_transport, "_PROCESS_MODE", "1")
    return accounts, owned, service, OwnedReviewWorker(service, ledger), admin, alice_user, alice, bob


def test_real_pipeline_writes_only_owner_store_and_preserves_rule_version(worker, monkeypatch, tmp_path):
    accounts, owned, service, runner, _, _, alice, bob = worker
    monkeypatch.setenv("DEEPSEEK_API_KEY", "")
    monkeypatch.setenv("ZHIPU_API_KEY", "")
    monkeypatch.setenv("XAI_API_KEY", "")
    shared = tmp_path / "unexpected-shared.sqlite"
    monkeypatch.setenv("STORE_DB_PATH", str(shared))
    raw = "采购合同：甲方向乙方购买设备。".encode()
    rid, job, secret = service.create(alice, filename="fiction.txt", category="procurement", raw=raw, require_precheck=False)
    assert runner.run(alice, job, secret, raw=raw) == rid
    review = owned.get(alice, rid)
    assert review["status"] == "done" and review["items"]
    assert review["rule_provenance"]["source"] == "database_test"
    assert review["rule_pack"]["rule_pack_id"] == "procurement"
    assert isinstance(review["evidence_index"], dict)
    with pytest.raises(ValueError, match="不能更改"):
        owned.mutate(alice, rid, lambda row: row["rule_provenance"].update(generation=999))
    assert owned.get(bob, rid) is None
    assert not shared.exists()
    with accounts._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM owned_review_worker_claims WHERE job_id=?", (job,)).fetchone()[0] == 1
    with pytest.raises(AuthorizationError):
        runner.run(alice, job, secret, raw=raw)


def test_worker_claim_prevents_duplicate_and_rechecks_revocation(worker, monkeypatch):
    import app.services.owned_review_worker as worker_module

    accounts, _, service, runner, admin, alice_user, alice, _ = worker
    rid, job, secret = service.create(alice, filename="fiction.txt", category="procurement", raw=b"test", require_precheck=False)
    entered, release = Event(), Event()

    def delayed(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return {"category": "procurement", "items": [], "error": ""}

    monkeypatch.setattr(worker_module, "run_review", delayed)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(runner.run, alice, job, secret, raw=b"test")
        assert entered.wait(10)
        try:
            with pytest.raises(AuthorizationError):
                runner.run(alice, job, secret, raw=b"test")
            accounts.set_active(admin, alice_user["id"], False)
        finally:
            release.set()
        with pytest.raises(AuthenticationError):
            first.result()
    assert service.reap_stale() == 1
    with accounts._connect() as conn:
        assert conn.execute("SELECT status FROM owned_review_jobs WHERE review_id=?", (rid,)).fetchone()["status"] == "failed"


def test_worker_requires_isolated_transport_mode(worker, monkeypatch):
    _, _, service, runner, _, _, alice, _ = worker
    _, job, secret = service.create(alice, filename="fiction.txt", category="procurement", raw=b"test", require_precheck=False)
    monkeypatch.setattr(owned_llm_transport, "_PROCESS_MODE", "0")
    with pytest.raises(RuntimeError, match="模式未启用"):
        runner.run(alice, job, secret, raw=b"test")


def test_worker_rejects_changed_content_and_stance(worker):
    _, owned, service, runner, _, _, alice, _ = worker
    rid, job, secret = service.create(alice, filename="fiction.txt", category="procurement", raw=b"original", require_precheck=False)
    with pytest.raises(ValueError, match="不一致"):
        runner.run(alice, job, secret, raw=b"altered")
    with pytest.raises(ValueError, match="不一致"):
        runner.run(alice, job, secret, raw=b"original", declared_stance="buyer")
    assert owned.get(alice, rid)["status"] == "processing"


def test_account_mode_rejects_same_database_before_store_initialization(tmp_path):
    import json
    import os
    import sqlite3
    import subprocess
    import sys

    path = tmp_path / "shared.sqlite"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE reviews (id TEXT PRIMARY KEY, created_at REAL NOT NULL, data TEXT NOT NULL)")
        conn.execute("INSERT INTO reviews VALUES (?,?,?)", ("example", 1, json.dumps({"status": "processing"})))
    env = os.environ.copy()
    env.update(OWNED_LLM_EXECUTION="1", ACCOUNT_DB_PATH=str(path), STORE_DB_PATH=str(path))
    proc = subprocess.run([sys.executable, "-c", "import app.services.store"], env=env, capture_output=True)
    assert proc.returncode != 0
    with sqlite3.connect(path) as conn:
        assert json.loads(conn.execute("SELECT data FROM reviews WHERE id='example'").fetchone()[0])["status"] == "processing"


def test_account_mode_does_not_initialize_or_open_shared_store(tmp_path):
    import json
    import os
    import sqlite3
    import subprocess
    import sys

    shared = tmp_path / "shared.sqlite"
    account = tmp_path / "account.sqlite"
    with sqlite3.connect(shared) as conn:
        conn.execute("CREATE TABLE reviews (id TEXT PRIMARY KEY, created_at REAL NOT NULL, data TEXT NOT NULL)")
        conn.execute("INSERT INTO reviews VALUES (?,?,?)", ("example", 1, json.dumps({"status": "processing"})))
    env = os.environ.copy()
    env.update(OWNED_LLM_EXECUTION="1", ACCOUNT_DB_PATH=str(account), STORE_DB_PATH=str(shared))
    script = "from app.services.store import ReviewStore; ReviewStore()"
    proc = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True)
    assert proc.returncode != 0
    script = "import sys; from pathlib import Path; from app.services.account_store import AccountStore; AccountStore(Path(sys.argv[1]))"
    proc = subprocess.run([sys.executable, "-c", script, str(shared)], env=env, capture_output=True)
    assert proc.returncode != 0
    with sqlite3.connect(shared) as conn:
        assert json.loads(conn.execute("SELECT data FROM reviews WHERE id='example'").fetchone()[0])["status"] == "processing"
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='account_users'").fetchone() is None


def test_worker_binding_cannot_be_rewritten_by_generic_mutation(worker):
    _, owned, service, _, _, _, alice, _ = worker
    rid, _, _ = service.create(alice, filename="fiction.txt", category="procurement", raw=b"original", require_precheck=False)
    for change in ({"input_sha256": "a" * 64}, {"input_stance": "buyer"}, {"filename": "other.txt"}, {"category": "lease"}):
        with pytest.raises(ValueError, match="不能更改"):
            owned.mutate(alice, rid, lambda row: row.update(change))
    legacy = owned.create(alice, filename="legacy.txt")
    with pytest.raises(ValueError, match="不能更改"):
        owned.mutate(alice, legacy, lambda row: row.update(input_sha256="a" * 64))


def test_real_pipeline_model_call_uses_account_context_and_ledger(worker, monkeypatch, tmp_path):
    from app.services import llm_ask

    accounts, owned, service, runner, _, _, alice, _ = worker
    monkeypatch.setattr(llm_ask, "_deepseek_key", lambda: "synthetic-key")
    monkeypatch.setattr(llm_ask, "_zhipu_key", lambda: None)
    monkeypatch.setattr(llm_ask, "_xai_key", lambda: None)
    monkeypatch.setenv("DEEPSEEK_MODEL_REVIEW", "deepseek-v4-flash")
    monkeypatch.setenv("DEEPSEEK_API_BASE", "https://api.deepseek.com")
    shared = tmp_path / "never-shared.sqlite"
    monkeypatch.setenv("STORE_DB_PATH", str(shared))
    calls = []

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"content": "{}"}}], "usage": {"prompt_tokens": 1, "completion_tokens": 1}}

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, *args, **kwargs):
            calls.append(True)
            return FakeResponse()

    monkeypatch.setattr(owned_llm_transport.httpx, "Client", FakeClient)
    raw = "采购合同：甲方向乙方购买设备。".encode()
    rid, job, secret = service.create(alice, filename="fiction.txt", category="procurement", raw=raw, require_precheck=False)
    assert runner.run(alice, job, secret, raw=raw) == rid
    assert owned.get(alice, rid)["status"] == "done" and calls
    with accounts._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM owned_llm_attempts WHERE job_id=?", (job,)).fetchone()[0] == len(calls)
    assert not shared.exists()


def test_account_mode_real_subprocess_pipeline_stays_in_account_database(worker, tmp_path):
    import json
    import os
    import subprocess
    import sys

    accounts, _, service, _, _, _, alice, _ = worker
    raw = "采购合同：甲方向乙方购买设备。".encode()
    rid, job, secret = service.create(alice, filename="fiction.txt", category="procurement", raw=raw, require_precheck=False)
    shared = tmp_path / "subprocess-shared.sqlite"
    env = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP",
                                             "APPDATA", "LOCALAPPDATA", "USERPROFILE") if key in os.environ}
    env.update(OWNED_LLM_EXECUTION="1", ACCOUNT_DB_PATH=str(accounts.path), STORE_DB_PATH=str(shared),
               DEEPSEEK_MODEL_REVIEW="deepseek-v4-flash", DEEPSEEK_API_BASE="https://api.deepseek.com",
               PRECHECK_ENABLED="false", QUALITY_ENABLED="false", OBJECTIONS_ENABLED="false", PYTHONIOENCODING="utf-8")
    script = r'''
import json, sys
from pathlib import Path
from app.services import llm_ask, owned_llm_transport
from app.services.account_store import AccountStore
from app.services.owned_review_store import OwnedReviewStore
from app.services.owned_review_jobs import OwnedReviewJobs
from app.services.owned_llm_ledger import OwnedLLMLedger
from app.services.owned_review_worker import OwnedReviewWorker
from app.services.store import ReviewStore
class Response:
    status_code = 200
    def json(self): return {"choices": [{"message": {"content": "{}"}}], "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
class Client:
    def __init__(self, **kwargs): pass
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def post(self, *args, **kwargs): return Response()
owned_llm_transport.httpx.Client = Client
llm_ask._deepseek_key = lambda: "synthetic-key"
llm_ask._zhipu_key = lambda: None
llm_ask._xai_key = lambda: None
payload = json.loads(sys.stdin.read())
accounts = AccountStore(Path(payload["path"]))
owned = OwnedReviewStore(accounts, ReviewStore(payload["path"]))
jobs = OwnedReviewJobs(owned)
runner = OwnedReviewWorker(jobs, OwnedLLMLedger(jobs))
rid = runner.run(payload["token"], payload["job"], payload["secret"], raw=payload["text"].encode())
print(json.dumps({"rid": rid, "status": owned.get(payload["token"], rid)["status"]}))
'''
    payload = {"path": str(accounts.path), "token": alice, "job": job, "secret": secret, "text": raw.decode()}
    proc = subprocess.run([sys.executable, "-X", "utf8", "-c", script], env=env, input=json.dumps(payload),
                          capture_output=True, text=True, encoding="utf-8", timeout=90)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout.strip()) == {"rid": rid, "status": "done"}
    with accounts._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM owned_llm_attempts WHERE job_id=?", (job,)).fetchone()[0] > 0
    assert not shared.exists()
