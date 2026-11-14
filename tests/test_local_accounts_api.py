"""Account API is an explicit local-only experiment, not full application auth."""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app
from app.services.account_store import AccountStore
from tests.helpers import create_active_account


def _client():
    return TestClient(app, base_url="http://127.0.0.1:8000", client=("127.0.0.1", 12345))


def test_local_accounts_default_off_and_reject_nonlocal(tmp_path, monkeypatch):
    monkeypatch.delenv("ACCOUNT_LOCAL_ENABLED", raising=False)
    with _client() as client:
        assert client.get("/api/local-accounts/capability").json()["available"] is False
        assert client.get("/api/local-accounts/session").status_code == 404
    monkeypatch.setenv("ACCOUNT_LOCAL_ENABLED", "1")
    monkeypatch.setenv("ACCOUNT_DB_PATH", str(tmp_path / "accounts.sqlite"))
    with TestClient(app, base_url="http://127.0.0.1:8000", client=("203.0.113.1", 12345)) as client:
        assert client.get("/api/local-accounts/session").status_code == 403
    with _client() as client:
        body = {"username": "admin", "display_name": "管理员", "password": "testing-password-123"}
        assert client.post("/api/local-accounts/bootstrap", json=body, headers={"Origin": "https://evil.example"}).status_code == 404
        assert client.post("/api/local-accounts/bootstrap", json=body, headers={"Origin": "http://127.0.0.1:8000"}).status_code == 404


def test_local_accounts_login_csrf_roles_and_revocation(tmp_path, monkeypatch):
    monkeypatch.setenv("ACCOUNT_LOCAL_ENABLED", "1")
    monkeypatch.setenv("ACCOUNT_DB_PATH", str(tmp_path / "accounts.sqlite"))
    AccountStore(tmp_path / "accounts.sqlite").bootstrap_admin("admin", "管理员", "testing-password-123")
    origin = {"Origin": "http://127.0.0.1:8000"}
    with _client() as admin, _client() as user:
        credentials = {"username": "admin", "password": "testing-password-123"}
        assert admin.post("/api/local-accounts/login", json=credentials, headers={"Origin": "https://evil.example"}).status_code == 403
        login = admin.post("/api/local-accounts/login", json={"username": "admin", "password": "testing-password-123"}, headers=origin)
        assert login.status_code == 200
        assert "HttpOnly" in login.headers["set-cookie"] and "SameSite=strict" in login.headers["set-cookie"]
        assert "Path=/api/local-accounts" in login.headers["set-cookie"]
        csrf = login.json()["csrf_token"]
        create = {"username": "alice", "display_name": "用户", "password": "alice-password-123", "role": "user"}
        assert admin.post("/api/local-accounts/users", json=create, headers=origin).status_code == 403
        assert admin.post("/api/local-accounts/users", json={**create, "actor": "admin"}, headers={**origin, "X-CSRF-Token": csrf}).status_code == 422
        created = admin.post("/api/local-accounts/users", json=create, headers={**origin, "X-CSRF-Token": csrf})
        assert created.status_code == 200
        user_id = created.json()["id"]
        user_login = user.post("/api/local-accounts/login", json={"username": "alice", "password": create["password"]}, headers=origin)
        assert user_login.status_code == 200
        assert user.get("/api/local-accounts/users").status_code == 403
        assert user.get("/api/local-accounts/users", headers={"X-CSRF-Token": user_login.json()["csrf_token"]}).status_code == 403
        assert admin.get("/api/local-accounts/users").status_code == 403
        assert admin.get("/api/local-accounts/users", headers={"X-CSRF-Token": csrf}).status_code == 200
        assert admin.post(f"/api/local-accounts/users/{user_id}/active", json={"active": False},
                          headers={**origin, "X-CSRF-Token": csrf}).status_code == 200
        assert user.get("/api/local-accounts/session").status_code == 401
        logged_out = admin.post("/api/local-accounts/logout", headers={**origin, "X-CSRF-Token": csrf})
        assert logged_out.status_code == 200
        assert "set-cookie" not in logged_out.headers
        assert admin.get("/api/local-accounts/session").status_code == 401


def test_password_endpoints_enforce_csrf_and_invalidate_session(tmp_path, monkeypatch):
    monkeypatch.setenv("ACCOUNT_LOCAL_ENABLED", "1")
    monkeypatch.setenv("ACCOUNT_DB_PATH", str(tmp_path / "accounts.sqlite"))
    store = AccountStore(tmp_path / "accounts.sqlite")
    store.bootstrap_admin("admin", "管理员", "testing-password-123")
    token = store.login("admin", "testing-password-123")
    user = create_active_account(store, token, "alice", "用户", "alice-password-123", "user")
    origin = {"Origin": "http://127.0.0.1:8000"}
    with _client() as admin, _client() as alice:
        admin_login = admin.post("/api/local-accounts/login", json={"username": "admin", "password": "testing-password-123"}, headers=origin)
        alice_login = alice.post("/api/local-accounts/login", json={"username": "alice", "password": "alice-password-123"}, headers=origin)
        admin_csrf, alice_csrf = admin_login.json()["csrf_token"], alice_login.json()["csrf_token"]
        reset = {"new_password": "reset-alice-password-123", "admin_password": "testing-password-123"}
        assert admin.post(f"/api/local-accounts/users/{user['id']}/password", json=reset, headers=origin).status_code == 403
        assert alice.post(f"/api/local-accounts/users/{user['id']}/password", json=reset, headers={**origin, "X-CSRF-Token": alice_csrf}).status_code == 403
        assert admin.post(f"/api/local-accounts/users/{user['id']}/password", json=reset, headers={**origin, "X-CSRF-Token": admin_csrf}).status_code == 200
        assert alice.get("/api/local-accounts/session").status_code == 401
