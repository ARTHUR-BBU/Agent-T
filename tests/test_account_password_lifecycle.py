"""Administrator-issued passwords are temporary and cannot grant normal access."""
import pytest
import sqlite3

from app.services.account_store import AccountStore, AuthenticationError, AuthorizationError


def test_temporary_password_requires_change_and_reset_restricts_again(tmp_path):
    store = AccountStore(tmp_path / "accounts.sqlite")
    store.bootstrap_admin("admin", "管理员", "testing-password-123")
    admin = store.login("admin", "testing-password-123")
    user = store.create_user(admin, "alice", "用户", "temporary-password-123", "user")
    token = store.login("alice", "temporary-password-123")
    assert store.session_info(token)["user"]["must_change_password"] is True
    with pytest.raises(AuthorizationError):
        store.authenticate(token)
    with pytest.raises(ValueError, match="不同"):
        store.change_password(token, "temporary-password-123", "temporary-password-123")
    store.change_password(token, "temporary-password-123", "alice-personal-password-123")
    personal = store.login("alice", "alice-personal-password-123")
    assert store.authenticate(personal)["id"] == user["id"]
    store.reset_password(admin, user["id"], "another-temporary-123", "testing-password-123")
    with pytest.raises(AuthenticationError):
        store.authenticate(personal)
    restricted = store.login("alice", "another-temporary-123")
    with pytest.raises(AuthorizationError):
        store.authenticate(restricted)


def test_temporary_password_expires_and_admin_can_issue_a_new_one(tmp_path, monkeypatch):
    from app.services import account_store

    store = AccountStore(tmp_path / "accounts.sqlite")
    store.bootstrap_admin("admin", "管理员", "testing-password-123")
    admin = store.login("admin", "testing-password-123")
    user = store.create_user(admin, "alice", "用户", "temporary-password-123", "user")
    original_time = account_store.time.time
    future = original_time() + 25 * 60 * 60
    monkeypatch.setattr(account_store.time, "time", lambda: future)
    with pytest.raises(AuthenticationError):
        store.login("alice", "temporary-password-123")
    # The original admin session has also expired and must be renewed.
    admin = store.login("admin", "testing-password-123")
    store.reset_password(admin, user["id"], "renewed-temporary-123", "testing-password-123")
    token = store.login("alice", "renewed-temporary-123")
    assert store.session_info(token)["user"]["must_change_password"] is True


def test_legacy_populated_account_store_requires_explicit_migration(tmp_path):
    path = tmp_path / "legacy.sqlite"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE account_users (id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE, display_name TEXT NOT NULL, password_hash TEXT NOT NULL, role TEXT NOT NULL, active INTEGER NOT NULL, created_at REAL NOT NULL)")
        conn.execute("INSERT INTO account_users VALUES ('old', 'olduser', '用户', 'old-hash', 'admin', 1, 0)")
    with pytest.raises(RuntimeError, match="安全迁移"):
        AccountStore(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT password_hash FROM account_users WHERE id='old'").fetchone()[0] == "old-hash"
        assert "must_change_password" not in {row[1] for row in conn.execute("PRAGMA table_info(account_users)")}
        assert {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")} == {"account_users"}


def test_temporary_administrator_does_not_count_as_last_usable_admin(tmp_path):
    store = AccountStore(tmp_path / "accounts.sqlite")
    first = store.bootstrap_admin("admin", "管理员", "testing-password-123")
    token = store.login("admin", "testing-password-123")
    temporary = store.create_user(token, "second", "第二管理员", "temporary-password-123", "admin")
    with pytest.raises(AuthorizationError, match="最后"):
        store.set_active(token, first["id"], False)
    store.set_active(token, temporary["id"], False)
