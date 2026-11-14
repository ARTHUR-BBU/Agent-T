"""Account-job ledger transactions; no provider network or transport integration."""
import pytest

from app.services.account_store import AuthenticationError, AuthorizationError
from app.services.owned_llm_ledger import OwnedLLMLedger
from tests.test_owned_review_jobs import jobs  # noqa: F401


def test_account_job_attempts_are_isolated_and_settle_once(jobs, tmp_path, monkeypatch):  # noqa: F811
    accounts, owned, service, _, _, alice, bob = jobs
    shared_path = tmp_path / "shared.sqlite"
    monkeypatch.setenv("STORE_DB_PATH", str(shared_path))
    ledger = OwnedLLMLedger(service)
    rid, job, secret = service.create(alice, filename="虚构合同", category="procurement")
    with pytest.raises(AuthorizationError):
        ledger.begin(bob, job, secret, node="model_review", provider="deepseek", model="deepseek-v4-flash")
    call = ledger.begin(alice, job, secret, node="model_review", provider="deepseek", model="deepseek-v4-flash")
    with pytest.raises(AuthorizationError):
        ledger.settle(bob, job, secret, call, outcome="success")
    ledger.settle(alice, job, secret, call, outcome="success", input_tokens=10, output_tokens=5)
    with pytest.raises(AuthorizationError):
        ledger.settle(alice, job, secret, call, outcome="timeout")
    with accounts._connect() as conn:
        rows = conn.execute("SELECT * FROM owned_llm_attempts").fetchall()
        assert len(rows) == 1 and rows[0]["outcome"] == "success" and rows[0]["input_tokens"] == 10
        assert secret not in str(tuple(rows[0])) and alice not in str(tuple(rows[0]))
    assert owned.get(alice, rid)["status"] == "processing"
    assert not shared_path.exists()


def test_revoked_attempt_remains_unknown_and_cannot_settle(jobs):  # noqa: F811
    accounts, _, service, admin, alice_user, alice, _ = jobs
    ledger = OwnedLLMLedger(service)
    _, job, secret = service.create(alice, filename="测试", category="procurement")
    call = ledger.begin(alice, job, secret, node="quality", provider="zhipu", model="glm-5.2")
    accounts.set_active(admin, alice_user["id"], False)
    with pytest.raises(AuthenticationError):
        ledger.settle(alice, job, secret, call, outcome="success")
    assert ledger.reconcile() == 1 and ledger.reconcile() == 0
    with accounts._connect() as conn:
        row = conn.execute("SELECT outcome,input_tokens,output_tokens FROM owned_llm_attempts WHERE id=?", (call,)).fetchone()
        assert tuple(row) == ("unknown", None, None)


def test_decision_business_settlement_is_separate_and_one_time(jobs):  # noqa: F811
    accounts, _, service, _, _, alice, _ = jobs
    ledger = OwnedLLMLedger(service)
    _, job, secret = service.create(alice, filename="测试", category="procurement")
    call = ledger.begin(alice, job, secret, node="category_suggest", provider="cloudflare", model="clef-flash")
    ledger.settle(alice, job, secret, call, outcome="success")
    ledger.settle_business(alice, job, secret, call, outcome="parse_failed")
    with pytest.raises(AuthorizationError):
        ledger.settle_business(alice, job, secret, call, outcome="success")
    with accounts._connect() as conn:
        row = conn.execute("SELECT outcome,business_outcome FROM owned_llm_attempts WHERE id=?", (call,)).fetchone()
        assert tuple(row) == ("success", "parse_failed")


def test_expired_review_cascades_ledger_and_attempts_reject_invalid_metadata(jobs):  # noqa: F811
    accounts, owned, service, _, _, alice, _ = jobs
    ledger = OwnedLLMLedger(service)
    owned.reviews._ttl = 10
    rid, job, secret = service.create(alice, filename="测试", category="procurement")
    with pytest.raises(ValueError):
        ledger.begin(alice, job, secret, node="model_review", provider="deepseek", model="合同原文")
    with pytest.raises(ValueError):
        ledger.begin(alice, job, secret, node="model_review", provider="deepseek", model="sk-sensitive-secret")
    call = ledger.begin(alice, job, secret, node="model_review", provider="deepseek", model="deepseek-v4-flash")
    with pytest.raises(ValueError):
        ledger.settle(alice, job, secret, call, outcome="success", input_tokens=True)
    with accounts._connect() as conn:
        conn.execute("UPDATE reviews SET created_at=0 WHERE id=?", (rid,))
    assert service.reap_stale() == 1
    with accounts._connect() as conn:
        assert conn.execute("SELECT 1 FROM owned_llm_attempts WHERE id=?", (call,)).fetchone() is None
