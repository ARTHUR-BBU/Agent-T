"""Authenticated job writes are atomic; no worker or public route is implied."""
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.services.account_store import AccountStore, AuthenticationError, AuthorizationError
from app.services.owned_review_jobs import OwnedReviewJobs
from app.services.owned_review_store import OwnedReviewStore
from app.services.store import ReviewStore
from tests.helpers import create_active_account


@pytest.fixture
def jobs(tmp_path):
    path = tmp_path / "isolated.sqlite"
    accounts = AccountStore(path)
    accounts.bootstrap_admin("admin", "管理员", "testing-password-123")
    admin = accounts.login("admin", "testing-password-123")
    alice = create_active_account(accounts, admin, "alice", "甲", "alice-password-123", "user")
    create_active_account(accounts, admin, "bobby", "乙", "bobby-password-123", "user")
    reviews = ReviewStore(str(path))
    owned = OwnedReviewStore(accounts, reviews)
    return accounts, owned, OwnedReviewJobs(owned), admin, alice, accounts.login("alice", "alice-password-123"), accounts.login("bobby", "bobby-password-123")


def test_job_binds_owner_session_and_worker_and_commits_once(jobs):
    accounts, owned, service, _, _, alice, bob = jobs
    rid, job, secret = service.create(alice, filename="测试合同", category="procurement")
    other_session = accounts.login("alice", "alice-password-123")
    for token, nonce in ((bob, secret), (alice, "wrong"), (other_session, secret)):
        with pytest.raises(AuthorizationError):
            service.commit(token, job, nonce, result={"items": [{"id": "one"}]})
    assert owned.get(bob, rid) is None
    assert owned.get(alice, rid)["status"] == "processing"
    assert service.commit(alice, job, secret, result={"items": [{"id": "one"}]}) == rid
    assert owned.get(alice, rid)["status"] == "done"
    with pytest.raises(AuthorizationError):
        service.commit(alice, job, secret, result={})
    with accounts._connect() as conn:
        row = conn.execute("SELECT * FROM owned_review_jobs WHERE id=?", (job,)).fetchone()
        assert secret not in tuple(str(value) for value in row)


def test_revocation_expiry_and_concurrent_completion_fail_closed(jobs, monkeypatch):
    import app.services.owned_review_jobs as job_module

    accounts, owned, service, admin, alice_user, alice, _ = jobs
    rid, job, secret = service.create(alice, filename="测试", category="procurement")
    with ThreadPoolExecutor(max_workers=2) as pool:
        def finish():
            try:
                return service.commit(alice, job, secret, result={"items": []})
            except AuthorizationError:
                return None
        assert list(pool.map(lambda _: finish(), range(2))).count(rid) == 1
    exp_id, exp_job, exp_secret = service.create(alice, filename="过期", category="procurement", lease_seconds=1)
    real_time = job_module.time.time
    monkeypatch.setattr(job_module.time, "time", lambda: real_time() + 2)
    with pytest.raises(AuthorizationError):
        service.commit(alice, exp_job, exp_secret, result={})
    monkeypatch.undo()
    assert owned.get(alice, exp_id)["status"] == "processing"
    _, revoked_job, revoked_secret = service.create(alice, filename="撤权", category="procurement")
    accounts.set_active(admin, alice_user["id"], False)
    with pytest.raises(AuthenticationError):
        service.commit(alice, revoked_job, revoked_secret, result={})


def test_rejects_uninitialized_database(tmp_path):
    path = tmp_path / "accounts.sqlite"
    accounts = AccountStore(path)
    owned = OwnedReviewStore(accounts, ReviewStore(str(path)))
    with accounts._connect() as conn:
        conn.execute("DROP TABLE reviews")
    with pytest.raises(RuntimeError, match="尚未初始化"):
        OwnedReviewJobs(owned)


def test_expired_review_cannot_commit_and_cleanup_removes_job(jobs):
    accounts, owned, _, _, _, alice, _ = jobs
    owned.reviews._ttl = 10
    service = OwnedReviewJobs(owned)
    rid, job, secret = service.create(alice, filename="测试", category="procurement")
    with accounts._connect() as conn:
        conn.execute("UPDATE reviews SET created_at=0 WHERE id=?", (rid,))
    with pytest.raises(AuthorizationError):
        service.commit(alice, job, secret, result={"items": []})
    owned.create(alice, filename="新的")
    with accounts._connect() as conn:
        assert conn.execute("SELECT 1 FROM reviews WHERE id=?", (rid,)).fetchone() is None
        assert conn.execute("SELECT 1 FROM owned_review_jobs WHERE id=?", (job,)).fetchone() is None


def test_result_cannot_override_trusted_metadata(jobs):
    _, owned, service, _, _, alice, _ = jobs
    rid, job, secret = service.create(alice, filename="原合同", category="procurement")
    with pytest.raises(ValueError, match="结果无效"):
        service.commit(alice, job, secret, result={"filename": "篡改"})
    assert owned.get(alice, rid)["filename"] == "原合同"


def test_rejects_old_job_table_without_cascading_cleanup(tmp_path):
    path = tmp_path / "old.sqlite"
    accounts = AccountStore(path)
    owned = OwnedReviewStore(accounts, ReviewStore(str(path)))
    with accounts._connect() as conn:
        conn.execute("""CREATE TABLE owned_review_jobs (
            id TEXT PRIMARY KEY, review_id TEXT REFERENCES reviews(id), owner_user_id TEXT,
            session_hash TEXT, worker_hash TEXT, status TEXT, created_at REAL, lease_until REAL)""")
    with pytest.raises(RuntimeError, match="离线迁移"):
        OwnedReviewJobs(owned)


def test_rejects_lookalike_table_without_required_constraints(tmp_path):
    path = tmp_path / "lookalike.sqlite"
    accounts = AccountStore(path)
    owned = OwnedReviewStore(accounts, ReviewStore(str(path)))
    with accounts._connect() as conn:
        conn.execute("""CREATE TABLE owned_review_jobs (
            id TEXT, review_id TEXT REFERENCES reviews(id) ON DELETE CASCADE, owner_user_id TEXT,
            session_hash TEXT, worker_hash TEXT, status TEXT, created_at REAL, lease_until REAL)""")
    with pytest.raises(RuntimeError, match="离线迁移"):
        OwnedReviewJobs(owned)


def test_reap_revoked_session_prevents_late_commit(jobs):
    import json

    accounts, _, service, admin, alice_user, alice, _ = jobs
    rid, job, secret = service.create(alice, filename="测试", category="procurement")
    accounts.set_active(admin, alice_user["id"], False)
    assert service.reap_stale() == 1
    assert service.reap_stale() == 0
    with pytest.raises(AuthenticationError):
        service.commit(alice, job, secret, result={"items": []})
    with accounts._connect() as conn:
        review = conn.execute("SELECT data FROM reviews WHERE id=?", (rid,)).fetchone()
        assert json.loads(review["data"])["status"] == "error"
        assert conn.execute("SELECT status FROM owned_review_jobs WHERE id=?", (job,)).fetchone()["status"] == "failed"


def test_reap_removes_expired_review_before_job_lease_expires(jobs):
    accounts, owned, service, _, _, alice, _ = jobs
    owned.reviews._ttl = 10
    rid, job, secret = service.create(alice, filename="测试", category="procurement", lease_seconds=300)
    with accounts._connect() as conn:
        conn.execute("UPDATE reviews SET created_at=0 WHERE id=?", (rid,))
    assert service.reap_stale() == 1
    with accounts._connect() as conn:
        assert conn.execute("SELECT 1 FROM reviews WHERE id=?", (rid,)).fetchone() is None
        assert conn.execute("SELECT 1 FROM owned_review_jobs WHERE id=?", (job,)).fetchone() is None
    with pytest.raises(AuthorizationError):
        service.commit(alice, job, secret, result={})


def test_outbound_authorization_checks_owner_session_nonce_and_revocation(jobs):
    accounts, _, service, admin, alice_user, alice, bob = jobs
    rid, job, secret = service.create(alice, filename="测试", category="procurement")
    assert service.authorize_outbound(alice, job, secret) == rid
    other_session = accounts.login("alice", "alice-password-123")
    for token, nonce in ((bob, secret), (alice, "wrong"), (other_session, secret)):
        with pytest.raises(AuthorizationError):
            service.authorize_outbound(token, job, nonce)
    accounts.set_active(admin, alice_user["id"], False)
    with pytest.raises(AuthenticationError):
        service.authorize_outbound(alice, job, secret)


def test_outbound_authorization_rejects_expired_review_and_terminal_job(jobs):
    accounts, owned, service, _, _, alice, _ = jobs
    owned.reviews._ttl = 10
    rid, job, secret = service.create(alice, filename="过期", category="procurement")
    with accounts._connect() as conn:
        conn.execute("UPDATE reviews SET created_at=0 WHERE id=?", (rid,))
    with pytest.raises(AuthorizationError):
        service.authorize_outbound(alice, job, secret)
    _, completed_job, completed_secret = service.create(alice, filename="完成", category="procurement")
    service.commit(alice, completed_job, completed_secret, result={"items": []})
    with pytest.raises(AuthorizationError):
        service.authorize_outbound(alice, completed_job, completed_secret)


def test_outbound_check_serializes_with_account_revocation(jobs, monkeypatch):
    from threading import Event

    accounts, _, service, admin, alice_user, alice, _ = jobs
    rid, job, secret = service.create(alice, filename="测试", category="procurement")
    entered, release, revoke_started = Event(), Event(), Event()
    original = accounts._session
    first = [True]

    def gated(conn, token, **kwargs):
        actor = original(conn, token, **kwargs)
        if token == alice and first[0]:
            first[0] = False
            entered.set()
            assert release.wait(10)
        return actor

    monkeypatch.setattr(accounts, "_session", gated)
    with ThreadPoolExecutor(max_workers=2) as pool:
        check = pool.submit(service.authorize_outbound, alice, job, secret)
        assert entered.wait(10)

        def revoke():
            revoke_started.set()
            accounts.set_active(admin, alice_user["id"], False)

        revocation = pool.submit(revoke)
        assert revoke_started.wait(10)
        assert not revocation.done()
        release.set()
        assert check.result() == rid
        revocation.result()
    with pytest.raises(AuthenticationError):
        service.authorize_outbound(alice, job, secret)


def test_outbound_authorization_rejects_expired_lease_and_session(jobs):
    from app.services.account_store import _token_digest

    accounts, _, service, _, _, alice, _ = jobs
    _, expired_job, secret = service.create(alice, filename="测试", category="procurement")
    with accounts._connect() as conn:
        conn.execute("UPDATE owned_review_jobs SET lease_until=0 WHERE id=?", (expired_job,))
    with pytest.raises(AuthorizationError):
        service.authorize_outbound(alice, expired_job, secret)
    _, active_job, secret = service.create(alice, filename="测试", category="procurement")
    with accounts._connect() as conn:
        conn.execute("UPDATE account_sessions SET expires_at=0 WHERE token_hash=?", (_token_digest(alice),))
    with pytest.raises(AuthenticationError):
        service.authorize_outbound(alice, active_job, secret)
