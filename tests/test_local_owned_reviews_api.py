"""Isolated read experiment; existing shared contract APIs remain unprotected."""
import time

from fastapi.testclient import TestClient

from app.main import app
from app.services.account_store import AccountStore
from app.services.owned_review_store import OwnedReviewStore
from app.services.store import ReviewStore
from tests.helpers import create_active_account


def client():
    return TestClient(app, base_url="http://127.0.0.1:8000", client=("127.0.0.1", 12345))


def test_isolated_owned_reads_authenticate_and_filter_before_limit(tmp_path, monkeypatch):
    path = tmp_path / "isolated.sqlite"
    accounts = AccountStore(path)
    accounts.bootstrap_admin("admin", "管理员", "testing-password-123")
    admin_token = accounts.login("admin", "testing-password-123")
    alice = create_active_account(accounts, admin_token, "alice", "甲", "alice-password-123", "user")
    create_active_account(accounts, admin_token, "bobby", "乙", "bobby-password-123", "user")
    reviews = ReviewStore(str(path))
    legacy = reviews.create(filename="旧无主合同", status="done")
    owned = OwnedReviewStore(accounts, reviews)
    alice_token = accounts.login("alice", "alice-password-123")
    bob_token = accounts.login("bobby", "bobby-password-123")
    alice_id = owned.create(alice_token, filename="甲合同", status="done", text="不返回原文", items=[{"id": "one"}],
                            precheck={"performed": False, "skip_reason": "disabled", "suggestion_performed": False,
                                      "raw_output": "不应返回", "original_category": "procurement"})
    bob_id = owned.create(bob_token, filename="乙合同", status="done")
    expired = owned.create(alice_token, filename="已过期", status="done")
    with accounts._connect() as conn:
        conn.execute("UPDATE reviews SET created_at=? WHERE id=?", (time.time() - 1000000, expired))
    monkeypatch.setenv("ACCOUNT_LOCAL_ENABLED", "1")
    monkeypatch.setenv("ACCOUNT_DB_PATH", str(path))
    monkeypatch.setenv("CONTRACT_ACCOUNT_READ_TRIAL", "1")
    origin = {"Origin": "http://127.0.0.1:8000"}
    with client() as a, client() as b, client() as admin:
        def login(c, name, password):
            r = c.post("/api/local-accounts/login", json={"username": name, "password": password}, headers=origin)
            assert r.status_code == 200
            return {"X-CSRF-Token": r.json()["csrf_token"]}
        ah = login(a, "alice", "alice-password-123")
        bh = login(b, "bobby", "bobby-password-123")
        mh = login(admin, "admin", "testing-password-123")
        url = "/api/local-accounts/contracts"
        denied = a.get(url)
        assert denied.status_code == 403 and denied.headers["cache-control"] == "no-store"
        assert a.get(url, headers=bh).status_code == 403
        result = a.get(url + "?limit=1", headers=ah)
        assert result.status_code == 200 and result.headers["cache-control"] == "no-store"
        assert [row["id"] for row in result.json()["reviews"]] == [alice_id]
        detail = a.get(f"{url}/{alice_id}", headers=ah)
        assert detail.status_code == 200 and detail.json()["items"] == [{"id": "one"}]
        assert "text" not in detail.json() and "owner_user_id" not in detail.json()
        assert detail.json()["precheck"]["performed"] is False
        assert detail.json()["precheck"]["skip_reason"] == "disabled"
        assert detail.json()["precheck"]["suspect"] is None
        assert "raw_output" not in detail.json()["precheck"]
        for rid in (bob_id, legacy, expired, "missing"):
            assert a.get(f"{url}/{rid}", headers=ah).status_code == 404
        assert admin.get(f"{url}/{alice_id}", headers=mh).status_code == 404
        assert b.get(f"{url}/{alice_id}", headers=bh).status_code == 404
        assert a.get(url + "?limit=0", headers=ah).headers["cache-control"] == "no-store"
        with accounts._connect() as conn:
            conn.execute("UPDATE reviews SET data=? WHERE id=?", ("invalid json", bob_id))
        corrupt = b.get(f"{url}/{bob_id}", headers=bh)
        assert corrupt.status_code == 503 and corrupt.headers["cache-control"] == "no-store"
        accounts.set_active(admin_token, alice["id"], False)
        assert a.get(url, headers=ah).status_code == 401
    with accounts._connect() as conn:
        assert conn.execute("SELECT 1 FROM reviews WHERE id=?", (expired,)).fetchone() is not None


def test_owned_read_is_default_off_and_loopback_only(tmp_path, monkeypatch):
    monkeypatch.setenv("ACCOUNT_LOCAL_ENABLED", "1")
    monkeypatch.setenv("ACCOUNT_DB_PATH", str(tmp_path / "account.sqlite"))
    monkeypatch.delenv("CONTRACT_ACCOUNT_READ_TRIAL", raising=False)
    with client() as c:
        assert c.get("/api/local-accounts/contracts").status_code == 404
    monkeypatch.setenv("CONTRACT_ACCOUNT_READ_TRIAL", "1")
    with TestClient(app, base_url="http://127.0.0.1:8000", client=("203.0.113.1", 1234)) as remote:
        assert remote.get("/api/local-accounts/contracts").status_code == 403
    with client() as c:
        assert c.get("/api/local-accounts/contracts", headers={"X-Forwarded-For": "127.0.0.1"}).status_code == 403
        assert c.get("/api/local-accounts/contracts").status_code == 503


def test_owned_read_rejects_shared_review_database(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from app.api import routes_local_accounts

    path = tmp_path / "shared.sqlite"
    path.touch()
    monkeypatch.setattr(routes_local_accounts, "review_store", SimpleNamespace(_path=str(path)))
    monkeypatch.setenv("ACCOUNT_LOCAL_ENABLED", "1")
    monkeypatch.setenv("ACCOUNT_DB_PATH", str(path))
    monkeypatch.setenv("CONTRACT_ACCOUNT_READ_TRIAL", "1")
    with client() as c:
        response = c.get("/api/local-accounts/contracts")
        assert response.status_code == 503 and response.headers["cache-control"] == "no-store"


def test_owned_read_path_failure_is_unavailable_and_not_cached(monkeypatch):
    from app.api import routes_local_owned_reviews

    monkeypatch.setenv("CONTRACT_ACCOUNT_READ_TRIAL", "1")

    def fail_guard(request):
        raise OSError("filesystem failure")

    monkeypatch.setattr(routes_local_owned_reviews, "_guard", fail_guard)
    with client() as c:
        response = c.get("/api/local-accounts/contracts")
        assert response.status_code == 503 and response.headers["cache-control"] == "no-store"


def test_owned_read_unexpected_error_is_not_cached(monkeypatch):
    from app.api import routes_local_owned_reviews

    monkeypatch.setenv("CONTRACT_ACCOUNT_READ_TRIAL", "1")

    def fail_guard(request):
        raise RuntimeError("unexpected test error")

    monkeypatch.setattr(routes_local_owned_reviews, "_guard", fail_guard)
    with client() as c:
        response = c.get("/api/local-accounts/contracts")
        assert response.status_code == 500 and response.headers["cache-control"] == "no-store"


def test_owned_read_error_after_response_start_is_not_sent_twice():
    import asyncio

    import pytest

    from app.main import OwnedReviewNoStoreMiddleware

    messages = []

    async def broken(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        raise RuntimeError("late error")

    async def send(message):
        messages.append(message)

    async def run():
        middleware = OwnedReviewNoStoreMiddleware(broken)
        with pytest.raises(RuntimeError, match="late error"):
            await middleware({"type": "http", "path": "/api/local-accounts/contracts"}, None, send)

    asyncio.run(run())
    assert len(messages) == 1
    assert (b"cache-control", b"no-store") in messages[0]["headers"]
