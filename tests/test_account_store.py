"""Account primitives stay isolated until the application has complete access control."""
from __future__ import annotations

import sqlite3

import pytest

from app.services.account_store import AccountStore, AuthenticationError, AuthorizationError
from tests.helpers import create_active_account


def test_password_change_and_admin_reset_revoke_sessions(tmp_path):
    store = AccountStore(tmp_path / "accounts.sqlite")
    admin = store.bootstrap_admin("admin", "管理员", "testing-password-123")
    admin_token = store.login("admin", "testing-password-123")
    alice = create_active_account(store, admin_token, "alice", "用户", "alice-password-123", "user")
    alice_token = store.login("alice", "alice-password-123")
    second_session = store.login("alice", "alice-password-123")
    with pytest.raises(AuthenticationError):
        store.change_password(alice_token, "wrong-password-123", "new-alice-password-123")
    assert store.authenticate(second_session)["id"] == alice["id"]
    with pytest.raises(AuthorizationError):
        store.reset_password(alice_token, admin["id"], "new-admin-password-123", "alice-password-123")
    store.change_password(alice_token, "alice-password-123", "new-alice-password-123")
    for token in (alice_token, second_session):
        with pytest.raises(AuthenticationError):
            store.authenticate(token)
    token = store.login("alice", "new-alice-password-123")
    with pytest.raises(AuthorizationError):
        store.reset_password(admin_token, admin["id"], "new-admin-password-123", "testing-password-123")
    with pytest.raises(AuthenticationError):
        store.reset_password(admin_token, alice["id"], "reset-alice-password-123", "wrong-password-123")
    store.reset_password(admin_token, alice["id"], "reset-alice-password-123", "testing-password-123")
    with pytest.raises(AuthenticationError):
        store.authenticate(token)
    reset_token = store.login("alice", "reset-alice-password-123")
    assert store.session_info(reset_token)["user"]["must_change_password"] is True
    with sqlite3.connect(store.path) as conn:
        actions = [row[0] for row in conn.execute("SELECT action FROM account_audit")]
    assert "change_password" in actions and "reset_password" in actions


def test_bad_password_change_does_not_hash_new_password(tmp_path, monkeypatch):
    from app.services import account_store

    store = AccountStore(tmp_path / "accounts.sqlite")
    store.bootstrap_admin("admin", "管理员", "testing-password-123")
    token = store.login("admin", "testing-password-123")
    calls = []
    original = account_store._password_hash

    def count_hash(password, **kwargs):
        calls.append(1)
        return original(password, **kwargs)

    monkeypatch.setattr(account_store, "_password_hash", count_hash)
    with pytest.raises(AuthenticationError):
        store.change_password(token, "wrong-password-123", "new-password-123")
    assert calls == []


def test_bootstrap_login_roles_and_sensitive_data_never_stored(tmp_path):
    path = tmp_path / "accounts.sqlite"
    store = AccountStore(path)
    admin = store.bootstrap_admin("admin", "管理员", "testing-password-123")
    with pytest.raises(AuthorizationError):
        store.bootstrap_admin("another", "另一个", "another-password-123")
    token = store.login("admin", "testing-password-123")
    assert store.authenticate(token)["id"] == admin["id"]
    user = create_active_account(store, token, "alice", "普通用户", "alice-password-123", "user")
    user_token = store.login("alice", "alice-password-123")
    assert store.authenticate(user_token)["role"] == "user"
    csrf = store.session_info(token)["csrf_token"]
    store.check_csrf(token, csrf)
    with pytest.raises(AuthorizationError):
        store.check_csrf(token, "wrong-csrf")
    with pytest.raises(AuthorizationError):
        store.create_user(user_token, "bob", "用户", "bob-password-12345", "admin")
    with pytest.raises(AuthorizationError):
        store.list_users(user_token)
    assert {row["username"] for row in store.list_users(token)} == {"admin", "alice"}
    assert all("password_hash" not in row for row in store.list_users(token))
    assert b"testing-password-123" not in path.read_bytes()
    assert token.encode() not in path.read_bytes()
    assert user["id"] != admin["id"]


def test_disabled_user_and_role_change_revoke_existing_sessions(tmp_path):
    store = AccountStore(tmp_path / "accounts.sqlite")
    store.bootstrap_admin("admin", "管理员", "testing-password-123")
    admin_token = store.login("admin", "testing-password-123")
    user = create_active_account(store, admin_token, "alice", "用户", "alice-password-123", "user")
    token = store.login("alice", "alice-password-123")
    store.set_role(admin_token, user["id"], "admin")
    with pytest.raises(AuthenticationError):
        store.authenticate(token)
    token = store.login("alice", "alice-password-123")
    store.set_active(admin_token, user["id"], False)
    with pytest.raises(AuthenticationError):
        store.authenticate(token)
    with pytest.raises(AuthenticationError):
        store.login("alice", "alice-password-123")


def test_last_admin_and_expired_sessions_are_protected(tmp_path, monkeypatch):
    store = AccountStore(tmp_path / "accounts.sqlite")
    admin = store.bootstrap_admin("admin", "管理员", "testing-password-123")
    token = store.login("admin", "testing-password-123")
    with pytest.raises(AuthorizationError):
        store.set_active(token, admin["id"], False)
    with pytest.raises(AuthorizationError):
        store.set_role(token, admin["id"], "user")
    with pytest.raises(ValueError):
        store.set_active(token, admin["id"], 0)  # type: ignore[arg-type]
    monkeypatch.setattr("app.services.account_store.time.time", lambda: 4102444800.0)
    with pytest.raises(AuthenticationError):
        store.authenticate(token)


def test_generic_login_failure_and_throttling(tmp_path):
    store = AccountStore(tmp_path / "accounts.sqlite")
    store.bootstrap_admin("admin", "管理员", "testing-password-123")
    with pytest.raises(AuthenticationError) as missing:
        store.login("missing", "wrong-password")
    with pytest.raises(AuthenticationError) as wrong:
        store.login("admin", "wrong-password")
    assert str(missing.value) == str(wrong.value)
    for _ in range(4):
        with pytest.raises(AuthenticationError):
            store.login("admin", "wrong-password")
    with pytest.raises(AuthenticationError):
        store.login("admin", "testing-password-123")


def test_logout_revokes_session_and_audit_records_role_change(tmp_path):
    path = tmp_path / "accounts.sqlite"
    store = AccountStore(path)
    store.bootstrap_admin("admin", "管理员", "testing-password-123")
    admin_token = store.login("admin", "testing-password-123")
    user = create_active_account(store, admin_token, "alice", "用户", "alice-password-123", "user")
    store.set_role(admin_token, user["id"], "admin")
    store.logout(admin_token)
    with pytest.raises(AuthenticationError):
        store.authenticate(admin_token)
    with sqlite3.connect(path) as conn:
        actions = [r[0] for r in conn.execute("SELECT action FROM account_audit")]
    assert any("role=user->admin" in action for action in actions)
    assert "logout" in actions


def test_global_login_budget_caps_unique_usernames(tmp_path, monkeypatch):
    monkeypatch.setattr("app.services.account_store._MAX_GLOBAL_ATTEMPTS", 3)
    store = AccountStore(tmp_path / "accounts.sqlite")
    store.bootstrap_admin("admin", "管理员", "testing-password-123")
    for name in ("unknown1", "unknown2", "unknown3", "unknown4"):
        with pytest.raises(AuthenticationError):
            store.login(name, "wrong-password")
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM account_login_attempts").fetchone()[0] == 3


def test_unknown_username_capacity_does_not_block_existing_admin(tmp_path, monkeypatch):
    monkeypatch.setattr("app.services.account_store._MAX_TRACKED_USERS", 2)
    store = AccountStore(tmp_path / "accounts.sqlite")
    store.bootstrap_admin("admin", "管理员", "testing-password-123")
    for name in ("unknown1", "unknown2"):
        with pytest.raises(AuthenticationError):
            store.login(name, "wrong-password")
    assert store.authenticate(store.login("admin", "testing-password-123"))["role"] == "admin"
    from app.services import account_store
    verify = account_store._verify_password
    calls = []

    def counted_verify(password, encoded):
        calls.append(True)
        return verify(password, encoded)

    monkeypatch.setattr(account_store, "_verify_password", counted_verify)
    with pytest.raises(AuthenticationError):
        store.login("unknown3", "wrong-password")
    assert calls, "unknown names must still perform the password check when tracking is full"


def test_unknown_username_capacity_is_atomic(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    from app.services import account_store

    monkeypatch.setattr(account_store, "_MAX_TRACKED_USERS", 1)
    store = AccountStore(tmp_path / "accounts.sqlite")
    store.bootstrap_admin("admin", "管理员", "testing-password-123")
    barrier = threading.Barrier(2)
    verify = account_store._verify_password

    def synchronized_verify(password, encoded):
        barrier.wait(timeout=10)
        return verify(password, encoded)

    monkeypatch.setattr(account_store, "_verify_password", synchronized_verify)

    def attempt(name):
        try:
            store.login(name, "wrong-password")
        except AuthenticationError:
            return "denied"
        return "unexpected"

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(attempt, ["unknown1", "unknown2"])) == ["denied", "denied"]
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM account_login_attempts").fetchone()[0] == 1


def test_new_account_does_not_inherit_preexisting_unknown_name_lockout(tmp_path):
    store = AccountStore(tmp_path / "accounts.sqlite")
    store.bootstrap_admin("admin", "管理员", "testing-password-123")
    admin_token = store.login("admin", "testing-password-123")
    for _ in range(5):
        with pytest.raises(AuthenticationError):
            store.login("newuser", "wrong-password")
    create_active_account(store, admin_token, "newuser", "用户", "newuser-password-123", "user")
    assert store.authenticate(store.login("newuser", "newuser-password-123"))["username"] == "newuser"


def test_concurrent_bootstrap_creates_only_one_admin(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    import threading

    store = AccountStore(tmp_path / "accounts.sqlite")
    barrier = threading.Barrier(2)

    def create(name):
        barrier.wait(timeout=10)
        try:
            store.bootstrap_admin(name, name, "testing-password-123")
            return "created"
        except AuthorizationError:
            return "denied"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(create, ["first", "second"]))
    assert sorted(results) == ["created", "denied"]


def test_concurrent_admin_deactivation_cannot_remove_all_admins(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    import threading

    store = AccountStore(tmp_path / "accounts.sqlite")
    first = store.bootstrap_admin("admin", "管理员", "testing-password-123")
    first_token = store.login("admin", "testing-password-123")
    second = create_active_account(store, first_token, "other", "管理员乙", "another-password-123", "admin")
    second_token = store.login("other", "another-password-123")
    barrier = threading.Barrier(2)

    def deactivate(pair):
        token, target = pair
        barrier.wait(timeout=10)
        try:
            store.set_active(token, target, False)
            return "changed"
        except (AuthenticationError, AuthorizationError):
            return "denied"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(deactivate, [(first_token, second["id"]), (second_token, first["id"])]))
    assert sorted(results) == ["changed", "denied"]
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM account_users WHERE role='admin' AND active=1").fetchone()[0] == 1
