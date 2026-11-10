"""Account-bound prechecks using synthetic text and fake model responses."""
import json

import pytest

from app.services import llm_ask, owned_llm_transport
from app.services.owned_llm_ledger import OwnedLLMLedger
from app.services.owned_review_precheck import confirm_account_intake, prepare_account_intake
from app.services.rule_snapshot import capture_rule_snapshot
from app.services.rule_versions import RuleVersionStore
from tests.test_owned_review_jobs import jobs  # noqa: F401


def test_precheck_uses_account_job_and_requests_confirmation(jobs, monkeypatch, tmp_path):  # noqa: F811
    accounts, owned, service, admin, _, alice, bob = jobs
    versions = RuleVersionStore(accounts)
    for category in ("procurement", "lease"):
        versions.initialize(admin, category, capture_rule_snapshot(category).config_bytes)
    ledger = OwnedLLMLedger(service)
    raw = "虚构合同：甲方向乙方租用场地。".encode()
    rid, job, secret = service.create(alice, filename="fiction.txt", category="procurement", raw=raw,
                                      input_kind="pasted_text", require_precheck=True)
    monkeypatch.setattr(owned_llm_transport, "_PROCESS_MODE", "1")
    monkeypatch.setenv("PRECHECK_ENABLED", "true")
    monkeypatch.setenv("CATEGORY_SUGGEST_ENABLED", "false")
    monkeypatch.setenv("DEEPSEEK_MODEL_PRECHECK", "deepseek-v4-flash")
    monkeypatch.setenv("DEEPSEEK_API_BASE", "https://api.deepseek.com")
    monkeypatch.setattr(llm_ask, "_deepseek_key", lambda: "fake")
    monkeypatch.setattr(llm_ask, "_zhipu_key", lambda: None)
    monkeypatch.setattr(llm_ask, "_xai_key", lambda: None)
    shared = tmp_path / "shared.sqlite"
    monkeypatch.setenv("STORE_DB_PATH", str(shared))

    class FakeResponse:
        status_code = 200

        def json(self):
            body = {"detected_type": "租赁合同", "is_supported": True, "suggested_category": "lease",
                    "confidence": "high", "summary": "请确认合同类型"}
            return {"choices": [{"message": {"content": json.dumps(body, ensure_ascii=False)}}]}

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, *args, **kwargs):
            return FakeResponse()

    monkeypatch.setattr(owned_llm_transport.httpx, "Client", FakeClient)
    from app.services.account_store import AuthorizationError
    from app.services.owned_review_worker import OwnedReviewWorker
    from app.services import owned_review_worker as worker_module
    runner = OwnedReviewWorker(service, ledger)
    with pytest.raises(AuthorizationError, match="预审尚未完成"):
        runner.run(alice, job, secret, raw=raw)
    result = prepare_account_intake(service, ledger, alice, job, secret, raw=raw)
    assert result.review_id == rid and result.action == "confirm" and result.suggested_category == "lease"
    assert result.precheck_performed is True and result.precheck_skip_reason is None
    assert {row[0] for row in result.versions} == {"procurement", "lease"}
    with accounts._connect() as conn:
        assert conn.execute("SELECT node FROM owned_llm_attempts WHERE job_id=?", (job,)).fetchone()["node"] == "precheck"
    with pytest.raises(AuthorizationError, match="已经预审"):
        prepare_account_intake(service, ledger, alice, job, secret, raw=raw)
    with pytest.raises(RuntimeError, match="确认尚未完成"):
        runner.run(alice, job, secret, raw=raw)
    with pytest.raises(AuthorizationError):
        confirm_account_intake(service, bob, job, secret, raw=raw, input_kind="pasted_text", category="lease")
    with pytest.raises(AuthorizationError):
        confirm_account_intake(service, alice, job, secret, raw=raw + b"x", input_kind="pasted_text", category="lease")
    with pytest.raises(AuthorizationError):
        confirm_account_intake(service, alice, job, secret, raw=raw, input_kind="file", category="lease")
    other_session = accounts.login("alice", "alice-password-123")
    with pytest.raises(AuthorizationError):
        confirm_account_intake(service, other_session, job, secret, raw=raw, input_kind="pasted_text", category="lease")
    with pytest.raises(AuthorizationError):
        confirm_account_intake(service, alice, job, secret, raw=raw, input_kind="pasted_text", category="nda")
    from concurrent.futures import ThreadPoolExecutor

    def choose():
        try:
            return confirm_account_intake(service, alice, job, secret, raw=raw, input_kind="pasted_text", category="lease")
        except AuthorizationError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        choices = list(pool.map(lambda _: choose(), range(2)))
    assert sum(value is not None for value in choices) == 1
    chosen_stance = next(value for value in choices if value is not None)
    with pytest.raises(AuthorizationError):
        confirm_account_intake(service, alice, job, secret, raw=raw, input_kind="pasted_text", category="lease")
    remaining = []

    def run_pipeline(*args, **kwargs):
        remaining.append(kwargs["budget"].remaining())
        return {"category": "lease", "items": [], "error": ""}

    monkeypatch.setattr(worker_module, "run_review", run_pipeline)
    assert runner.run(alice, job, secret, raw=raw, declared_stance=chosen_stance) == rid
    assert remaining == [result.budget.remaining()]
    review = owned.get(alice, rid)
    assert review["category"] == "lease" and review["rule_provenance"]["category"] == "lease"
    assert review["precheck"]["suspect"] is True
    assert not shared.exists()


def test_precheck_rejects_changed_document_or_revoked_user(jobs, monkeypatch):  # noqa: F811
    from app.services.account_store import AuthenticationError

    accounts, _, service, admin, alice_user, alice, _ = jobs
    RuleVersionStore(accounts).initialize(admin, "procurement", capture_rule_snapshot("procurement").config_bytes)
    ledger = OwnedLLMLedger(service)
    _, job, secret = service.create(alice, filename="fiction.txt", category="procurement", raw=b"original")
    monkeypatch.setattr(owned_llm_transport, "_PROCESS_MODE", "1")
    with pytest.raises(ValueError, match="不一致"):
        prepare_account_intake(service, ledger, alice, job, secret, raw=b"changed")
    accounts.set_active(admin, alice_user["id"], False)
    with pytest.raises(AuthenticationError):
        prepare_account_intake(service, ledger, alice, job, secret, raw=b"original")


def test_budget_failure_does_not_leak_precheck_slots(jobs, monkeypatch):  # noqa: F811
    from app.services import owned_review_precheck

    accounts, _, service, admin, _, alice, _ = jobs
    RuleVersionStore(accounts).initialize(admin, "procurement", capture_rule_snapshot("procurement").config_bytes)
    ledger = OwnedLLMLedger(service)
    _, job, secret = service.create(alice, filename="fiction.txt", category="procurement", raw=b"original")
    monkeypatch.setattr(owned_llm_transport, "_PROCESS_MODE", "1")

    def broken_budget():
        raise ValueError("test budget failure")

    original = owned_review_precheck.llm_budget.new_review_budget
    monkeypatch.setattr(owned_review_precheck.llm_budget, "new_review_budget", broken_budget)
    for _ in range(3):
        with pytest.raises(ValueError, match="test budget failure"):
            prepare_account_intake(service, ledger, alice, job, secret, raw=b"original")
    monkeypatch.setattr(owned_review_precheck.llm_budget, "new_review_budget", original)
    prepared = prepare_account_intake(service, ledger, alice, job, secret, raw=b"original")
    assert prepared.action == "proceed" and prepared.precheck_performed is False
    assert prepared.precheck_skip_reason == "disabled"


def test_running_worker_cannot_start_precheck_for_same_job(jobs, monkeypatch):  # noqa: F811
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from app.services import owned_review_worker as worker_module
    from app.services.account_store import AuthorizationError
    from app.services.owned_review_worker import OwnedReviewWorker

    accounts, _, service, admin, _, alice, _ = jobs
    RuleVersionStore(accounts).initialize(admin, "procurement", capture_rule_snapshot("procurement").config_bytes)
    ledger = OwnedLLMLedger(service)
    _, job, secret = service.create(alice, filename="fiction.txt", category="procurement", raw=b"original", require_precheck=False)
    monkeypatch.setattr(owned_llm_transport, "_PROCESS_MODE", "1")
    entered, release = Event(), Event()

    def delayed(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return {"category": "procurement", "items": [], "error": ""}

    monkeypatch.setattr(worker_module, "run_review", delayed)
    runner = OwnedReviewWorker(service, ledger)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(runner.run, alice, job, secret, raw=b"original")
        assert entered.wait(10)
        try:
            with pytest.raises(AuthorizationError, match="已经启动"):
                prepare_account_intake(service, ledger, alice, job, secret, raw=b"original")
        finally:
            release.set()
        assert future.result()


def test_precheck_rejects_rule_change_before_model_work(jobs, monkeypatch):  # noqa: F811
    from app.services import owned_review_precheck as module

    accounts, _, service, admin, _, alice, _ = jobs
    RuleVersionStore(accounts).initialize(admin, "procurement", capture_rule_snapshot("procurement").config_bytes)
    ledger = OwnedLLMLedger(service)
    _, job, secret = service.create(alice, filename="fiction.txt", category="procurement", raw=b"original")
    monkeypatch.setattr(owned_llm_transport, "_PROCESS_MODE", "1")
    original = module.read_trial_versions

    def change_after_capture(path):
        snapshot = original(path)
        with accounts._connect() as conn:
            conn.execute("UPDATE rule_active_versions SET generation=generation+1 WHERE category='procurement'")
        return snapshot

    monkeypatch.setattr(module, "read_trial_versions", change_after_capture)
    with pytest.raises(ValueError, match="预审前规则版本已变化"):
        prepare_account_intake(service, ledger, alice, job, secret, raw=b"original")
    with accounts._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM owned_llm_attempts").fetchone()[0] == 0


def test_precheck_rejects_rule_change_during_extraction(jobs, monkeypatch):  # noqa: F811
    from app.services import owned_review_precheck as module

    accounts, _, service, admin, _, alice, _ = jobs
    RuleVersionStore(accounts).initialize(admin, "procurement", capture_rule_snapshot("procurement").config_bytes)
    ledger = OwnedLLMLedger(service)
    _, job, secret = service.create(alice, filename="fiction.txt", category="procurement", raw=b"original")
    monkeypatch.setattr(owned_llm_transport, "_PROCESS_MODE", "1")
    sent = []
    monkeypatch.setattr(module.category_suggest, "suggest_category", lambda *args: sent.append(True))

    def change_during_extraction(filename, raw):
        with accounts._connect() as conn:
            conn.execute("UPDATE rule_active_versions SET generation=generation+1 WHERE category='procurement'")
        return "虚构合同"

    monkeypatch.setattr(module, "extract_text", change_during_extraction)
    with pytest.raises(ValueError, match="预审期间规则版本已变化"):
        prepare_account_intake(service, ledger, alice, job, secret, raw=b"original")
    assert sent == []


def test_suggested_category_without_neutral_never_selects_a_party(jobs, monkeypatch):  # noqa: F811
    import hashlib
    import time
    import uuid

    import yaml

    from app.services import precheck
    from app.services.account_store import AuthorizationError

    accounts, _, service, admin, _, alice, _ = jobs
    versions = RuleVersionStore(accounts)
    versions.initialize(admin, "procurement", capture_rule_snapshot("procurement").config_bytes)
    versions.initialize(admin, "lease", capture_rule_snapshot("lease").config_bytes)
    lease = capture_rule_snapshot("lease").configuration()
    lease["stances"]["allowed"] = ["lessee"]
    content = yaml.safe_dump(lease, allow_unicode=True).encode()
    version_id = uuid.uuid4().hex
    with accounts._connect() as conn:
        conn.execute("INSERT INTO rule_versions VALUES (?,?,?,?,?,?)", (version_id, "lease", hashlib.sha256(content).hexdigest(),
                     content, accounts.authenticate(admin)["id"], time.time()))
        conn.execute("UPDATE rule_active_versions SET version_id=?, generation=generation+1 WHERE category='lease'", (version_id,))
    ledger = OwnedLLMLedger(service)
    raw = b"synthetic contract"
    _, job, secret = service.create(alice, filename="fiction.txt", category="procurement", raw=raw)
    monkeypatch.setattr(owned_llm_transport, "_PROCESS_MODE", "1")
    monkeypatch.setattr(precheck, "run_precheck", lambda *args, **kwargs: precheck.PrecheckOutcome(
        performed=True, result=precheck.PrecheckResult(detected_type="租赁", is_supported=True,
            suggested_category="lease", confidence="high", summary="请确认")))
    prepared = prepare_account_intake(service, ledger, alice, job, secret, raw=raw)
    assert prepared.action == "confirm" and prepared.suggested_category is None
    with pytest.raises(AuthorizationError):
        confirm_account_intake(service, alice, job, secret, raw=raw, input_kind="file", category="lease")
    assert confirm_account_intake(service, alice, job, secret, raw=raw, input_kind="file", category="procurement") == "neutral"


def test_ready_precheck_cannot_run_after_rule_version_changes(jobs, monkeypatch):  # noqa: F811
    from app.services.account_store import AuthorizationError
    from app.services.owned_review_worker import OwnedReviewWorker

    accounts, _, service, admin, _, alice, _ = jobs
    RuleVersionStore(accounts).initialize(admin, "procurement", capture_rule_snapshot("procurement").config_bytes)
    ledger = OwnedLLMLedger(service)
    raw = b"fictional contract"
    _, job, secret = service.create(alice, filename="fiction.txt", category="procurement", raw=raw)
    monkeypatch.setattr(owned_llm_transport, "_PROCESS_MODE", "1")
    assert prepare_account_intake(service, ledger, alice, job, secret, raw=raw).action == "proceed"
    runner = OwnedReviewWorker(service, ledger)
    with accounts._connect() as conn:
        conn.execute("UPDATE rule_active_versions SET generation=generation+1 WHERE category='procurement'")
    with pytest.raises(AuthorizationError, match="规则版本已变化"):
        runner.run(alice, job, secret, raw=raw)
    with accounts._connect() as conn:
        assert conn.execute("SELECT state FROM owned_review_precheck_results WHERE job_id=?", (job,)).fetchone()[0] == "ready"
        assert conn.execute("SELECT 1 FROM owned_review_worker_claims WHERE job_id=?", (job,)).fetchone() is None


def test_confirmation_rejects_rule_change_without_changing_category(jobs, monkeypatch):  # noqa: F811
    from app.services import precheck
    from app.services.account_store import AuthorizationError

    accounts, owned, service, admin, _, alice, _ = jobs
    versions = RuleVersionStore(accounts)
    for category in ("procurement", "lease"):
        versions.initialize(admin, category, capture_rule_snapshot(category).config_bytes)
    ledger = OwnedLLMLedger(service)
    raw = b"fictional contract"
    rid, job, secret = service.create(alice, filename="fiction.txt", category="procurement", raw=raw)
    monkeypatch.setattr(owned_llm_transport, "_PROCESS_MODE", "1")
    monkeypatch.setattr(precheck, "run_precheck", lambda *args, **kwargs: precheck.PrecheckOutcome(
        performed=True, result=precheck.PrecheckResult(detected_type="租赁", is_supported=True,
            suggested_category="lease", confidence="high", summary="请确认")))
    assert prepare_account_intake(service, ledger, alice, job, secret, raw=raw).action == "confirm"
    with accounts._connect() as conn:
        conn.execute("UPDATE rule_active_versions SET generation=generation+1 WHERE category='lease'")
    with pytest.raises(AuthorizationError, match="规则版本"):
        confirm_account_intake(service, alice, job, secret, raw=raw, input_kind="file", category="lease")
    assert owned.get(alice, rid)["category"] == "procurement"
