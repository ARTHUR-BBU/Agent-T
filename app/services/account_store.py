"""Isolated account primitives; not yet wired to application authentication.

This module does not protect contract data or make the application multi-user.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import tempfile
import threading
import time
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any

_ACCOUNT_MODE = os.getenv("OWNED_LLM_EXECUTION", "0")
_ACCOUNT_PATH = os.getenv("ACCOUNT_DB_PATH", "").strip()
_SHARED_PATH = os.getenv("STORE_DB_PATH") or str(Path(tempfile.gettempdir()) / "agent-t-reviews.db")


class AuthenticationError(Exception):
    """A credential or session is invalid, expired, disabled or rate limited."""


class AuthorizationError(Exception):
    """The authenticated account is not allowed to perform the operation."""


_N, _R, _P = 2**14, 8, 5
_SESSION_SECONDS = 8 * 60 * 60
_TEMP_PASSWORD_SECONDS = 24 * 60 * 60
_LOCK_SECONDS = 15 * 60
_MAX_FAILURES = 5
_MAX_GLOBAL_ATTEMPTS = 50
_MAX_TRACKED_USERS = 10000
_ATTEMPT_RETENTION_SECONDS = 24 * 60 * 60
_HASH_SLOTS = threading.BoundedSemaphore(2)
_LOGIN_ERROR = "账号或密码无效，或暂时无法登录"


def _password_hash(password: str, *, salt: bytes | None = None) -> str:
    encoded = password.encode("utf-8")
    if len(password) < 12 or len(encoded) > 256:
        raise ValueError("密码至少 12 个字符，且不能超过 256 字节")
    salt = salt or secrets.token_bytes(16)
    if not _HASH_SLOTS.acquire(blocking=False):
        raise AuthenticationError(_LOGIN_ERROR)
    try:
        digest = hashlib.scrypt(encoded, salt=salt, n=_N, r=_R, p=_P, dklen=32, maxmem=64 * 1024 * 1024)
    finally:
        _HASH_SLOTS.release()
    return f"scrypt${_N}${_R}${_P}$" + base64.b64encode(salt).decode("ascii") + "$" + base64.b64encode(digest).decode("ascii")


def _verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, n, r, p, salt_text, digest_text = encoded.split("$")
        if (algorithm, n, r, p) != ("scrypt", str(_N), str(_R), str(_P)):
            return False
        salt = base64.b64decode(salt_text, validate=True)
        expected = base64.b64decode(digest_text, validate=True)
        if len(salt) != 16 or len(expected) != 32 or len(password.encode("utf-8")) > 256:
            return False
        actual = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_N, r=_R, p=_P,
                                dklen=32, maxmem=64 * 1024 * 1024)
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


_DUMMY_HASH = _password_hash("agent-t-dummy-credential")


def _username(value: str) -> str:
    value = value.strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{2,63}", value):
        raise ValueError("账号须为 3–64 位英文、数字、点、横线或下划线")
    return value


def _token_digest(token: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
        raise AuthenticationError(_LOGIN_ERROR)
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def _public(row: sqlite3.Row) -> dict[str, Any]:
    result = {key: row[key] for key in ("id", "username", "display_name", "role", "active")}
    result["must_change_password"] = bool(row["must_change_password"])
    return result


class AccountStore:
    """SQLite account/session store. Bootstrap is an offline trusted operation only."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        if not self.path.is_absolute():
            raise ValueError("账户数据库必须使用绝对路径")
        if _ACCOUNT_MODE == "1":
            if not _ACCOUNT_PATH or not Path(_ACCOUNT_PATH).is_absolute():
                raise RuntimeError("账户模式必须配置独立账户数据库")
            selected, configured = self.path.resolve(), Path(_ACCOUNT_PATH).resolve()
            if selected != configured and not (selected.exists() and configured.exists() and os.path.samefile(selected, configured)):
                raise RuntimeError("账户数据库与启动配置不一致")
            shared = Path(_SHARED_PATH).resolve()
            if selected == shared or (selected.exists() and shared.exists() and os.path.samefile(selected, shared)):
                raise RuntimeError("账户数据库必须独立于共享审查数据库")
        with closing(self._connect()) as conn, conn:
            existing = conn.execute("PRAGMA table_info(account_users)").fetchall()
            existing_columns = {row["name"] for row in existing}
            if (existing and not {"must_change_password", "password_expires_at"}.issubset(existing_columns)
                    and conn.execute("SELECT 1 FROM account_users LIMIT 1").fetchone()):
                raise RuntimeError("旧版账户数据库需要安全迁移；请勿直接启用")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS account_users (
                    id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE,
                    display_name TEXT NOT NULL, password_hash TEXT NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('user','admin')),
                    active INTEGER NOT NULL CHECK(active IN (0,1)), created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS account_sessions (
                    token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES account_users(id),
                    expires_at REAL NOT NULL, csrf_token TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS account_login_attempts (
                    username TEXT PRIMARY KEY, failures INTEGER NOT NULL, locked_until REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS account_login_budget (
                    id INTEGER PRIMARY KEY CHECK(id=1), window_start REAL NOT NULL, attempts INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS account_audit (
                    id TEXT PRIMARY KEY, actor_id TEXT, action TEXT NOT NULL,
                    target_id TEXT, created_at REAL NOT NULL
                );
            """)
            conn.execute("BEGIN IMMEDIATE")
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(account_users)")}
            if (not {"must_change_password", "password_expires_at"}.issubset(columns)
                    and conn.execute("SELECT 1 FROM account_users LIMIT 1").fetchone()):
                raise RuntimeError("旧版账户数据库需要安全迁移；请勿直接启用")
            if "must_change_password" not in columns:
                conn.execute("ALTER TABLE account_users ADD COLUMN must_change_password INTEGER NOT NULL DEFAULT 0")
            if "password_expires_at" not in columns:
                conn.execute("ALTER TABLE account_users ADD COLUMN password_expires_at REAL")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @staticmethod
    def _audit(conn: sqlite3.Connection, actor_id: str | None, action: str, target_id: str | None) -> None:
        conn.execute("INSERT INTO account_audit VALUES (?,?,?,?,?)",
                     (uuid.uuid4().hex, actor_id, action, target_id, time.time()))

    @staticmethod
    def _session(conn: sqlite3.Connection, token: str, *, admin: bool = False,
                 allow_restricted: bool = False) -> sqlite3.Row:
        row = conn.execute(
            "SELECT u.* FROM account_sessions s JOIN account_users u ON u.id=s.user_id "
            "WHERE s.token_hash=? AND s.expires_at>? AND u.active=1",
            (_token_digest(token), time.time()),
        ).fetchone()
        if row is None:
            raise AuthenticationError(_LOGIN_ERROR)
        if row["must_change_password"]:
            if row["password_expires_at"] is None or row["password_expires_at"] <= time.time():
                raise AuthenticationError(_LOGIN_ERROR)
            if not allow_restricted:
                raise AuthorizationError("请先修改临时密码")
        if admin and row["role"] != "admin":
            raise AuthorizationError("无权管理账户")
        return row

    @staticmethod
    def _insert_user(conn: sqlite3.Connection, username: str, display_name: str,
                     password_hash: str, role: str, *, temporary: bool = False) -> sqlite3.Row:
        if role not in {"user", "admin"}:
            raise ValueError("未知账户角色")
        if not display_name.strip() or len(display_name) > 100:
            raise ValueError("请输入不超过 100 字的姓名")
        user_id = uuid.uuid4().hex
        normalized_name = _username(username)
        try:
            now = time.time()
            conn.execute("INSERT INTO account_users (id, username, display_name, password_hash, role, active, created_at, must_change_password, password_expires_at) VALUES (?,?,?,?,?,?,?,?,?)",
                         (user_id, normalized_name, display_name.strip(), password_hash, role, 1, now,
                          int(temporary), now + _TEMP_PASSWORD_SECONDS if temporary else None))
        except sqlite3.IntegrityError as exc:
            raise ValueError("账号已存在") from exc
        conn.execute("DELETE FROM account_login_attempts WHERE username=?", (normalized_name,))
        row = conn.execute("SELECT * FROM account_users WHERE id=?", (user_id,)).fetchone()
        assert row is not None
        return row

    def bootstrap_admin(self, username: str, display_name: str, password: str) -> dict[str, Any]:
        hashed = _password_hash(password)
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT 1 FROM account_users LIMIT 1").fetchone():
                raise AuthorizationError("账户系统已初始化")
            row = self._insert_user(conn, username, display_name, hashed, "admin")
            self._audit(conn, row["id"], "bootstrap", row["id"])
            return _public(row)

    def create_user(self, token: str, username: str, display_name: str, password: str,
                    role: str) -> dict[str, Any]:
        self.authenticate(token, admin=True)
        hashed = _password_hash(password)
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            actor = self._session(conn, token, admin=True)
            row = self._insert_user(conn, username, display_name, hashed, role, temporary=True)
            self._audit(conn, actor["id"], f"create_user:{role}", row["id"])
            return _public(row)

    def login(self, username: str, password: str) -> str:
        try:
            name = _username(username)
        except ValueError:
            name = "\x00invalid"
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            now = time.time()
            conn.execute("DELETE FROM account_login_attempts WHERE updated_at<?", (now - _ATTEMPT_RETENTION_SECONDS,))
            conn.execute("DELETE FROM account_sessions WHERE expires_at<=?", (now,))
            budget = conn.execute("SELECT * FROM account_login_budget WHERE id=1").fetchone()
            if not budget or now - budget["window_start"] >= 60:
                conn.execute("INSERT OR REPLACE INTO account_login_budget VALUES (1,?,1)", (now,))
            elif budget["attempts"] >= _MAX_GLOBAL_ATTEMPTS:
                raise AuthenticationError(_LOGIN_ERROR)
            else:
                conn.execute("UPDATE account_login_budget SET attempts=attempts+1 WHERE id=1")
        with closing(self._connect()) as conn:
            now = time.time()
            attempt = conn.execute("SELECT * FROM account_login_attempts WHERE username=?", (name,)).fetchone()
            if attempt and attempt["locked_until"] > now:
                raise AuthenticationError(_LOGIN_ERROR)
            row = conn.execute("SELECT * FROM account_users WHERE username=?", (name,)).fetchone()
            saved_hash = row["password_hash"] if row else _DUMMY_HASH
        if not _HASH_SLOTS.acquire(blocking=False):
            raise AuthenticationError(_LOGIN_ERROR)
        try:
            verified = _verify_password(password, saved_hash)
        finally:
            _HASH_SLOTS.release()
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            now = time.time()
            attempt = conn.execute("SELECT * FROM account_login_attempts WHERE username=?", (name,)).fetchone()
            if attempt and attempt["locked_until"] > now:
                raise AuthenticationError(_LOGIN_ERROR)
            current = conn.execute("SELECT * FROM account_users WHERE username=?", (name,)).fetchone()
            if (not verified or not current or not current["active"] or current["password_hash"] != saved_hash
                    or (current["must_change_password"] and
                        (current["password_expires_at"] is None or current["password_expires_at"] <= now))):
                full_unknown = (not current and not attempt and conn.execute(
                    "SELECT COUNT(*) FROM account_login_attempts"
                ).fetchone()[0] >= _MAX_TRACKED_USERS)
                if not full_unknown:
                    failures = (attempt["failures"] if attempt and attempt["locked_until"] == 0 else 0) + 1
                    locked_until = now + _LOCK_SECONDS if failures >= _MAX_FAILURES else 0
                    conn.execute("INSERT OR REPLACE INTO account_login_attempts VALUES (?,?,?,?)", (name, failures, locked_until, now))
                    self._audit(conn, None, "login_failed", current["id"] if current else None)
                # Commit failures before raising outside the transaction.
                failed = True
            else:
                conn.execute("DELETE FROM account_login_attempts WHERE username=?", (name,))
                token = secrets.token_urlsafe(32)
                csrf_token = secrets.token_urlsafe(32)
                conn.execute("INSERT INTO account_sessions VALUES (?,?,?,?)",
                             (_token_digest(token), current["id"], now + _SESSION_SECONDS, csrf_token))
                self._audit(conn, current["id"], "login", current["id"])
                failed = False
        if failed:
            raise AuthenticationError(_LOGIN_ERROR)
        return token

    def authenticate(self, token: str, *, admin: bool = False, allow_restricted: bool = False) -> dict[str, Any]:
        with closing(self._connect()) as conn:
            return _public(self._session(conn, token, admin=admin, allow_restricted=allow_restricted))

    def session_info(self, token: str) -> dict[str, Any]:
        with closing(self._connect()) as conn:
            conn.execute("BEGIN")
            actor = self._session(conn, token, allow_restricted=True)
            row = conn.execute("SELECT csrf_token FROM account_sessions WHERE token_hash=?", (_token_digest(token),)).fetchone()
            if row is None:
                raise AuthenticationError(_LOGIN_ERROR)
            return {"user": _public(actor), "csrf_token": row["csrf_token"]}

    def check_csrf(self, token: str, csrf_token: str) -> None:
        expected = self.session_info(token)["csrf_token"]
        if (not isinstance(csrf_token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", csrf_token)
                or not hmac.compare_digest(csrf_token, expected)):
            raise AuthorizationError("请求验证失败")

    def list_users(self, token: str) -> list[dict[str, Any]]:
        with closing(self._connect()) as conn:
            self._session(conn, token, admin=True)
            rows = conn.execute("SELECT * FROM account_users ORDER BY username").fetchall()
            return [_public(row) for row in rows]

    def logout(self, token: str) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            actor = self._session(conn, token, allow_restricted=True)
            conn.execute("DELETE FROM account_sessions WHERE token_hash=?", (_token_digest(token),))
            self._audit(conn, actor["id"], "logout", actor["id"])

    def change_password(self, token: str, current_password: str, new_password: str) -> None:
        actor = self.authenticate(token, allow_restricted=True)
        if hmac.compare_digest(current_password.encode("utf-8"), new_password.encode("utf-8")):
            raise ValueError("新密码必须与原密码不同")
        # Reuse the login throttle and password verifier for the reauthentication step.
        reauth_token = self.login(actor["username"], current_password)
        try:
            hashed = _password_hash(new_password)
            with closing(self._connect()) as conn, conn:
                conn.execute("BEGIN IMMEDIATE")
                current = self._session(conn, token, allow_restricted=True)
                reauthenticated = self._session(conn, reauth_token, allow_restricted=True)
                if current["id"] != reauthenticated["id"] or current["id"] != actor["id"]:
                    raise AuthenticationError(_LOGIN_ERROR)
                conn.execute("UPDATE account_users SET password_hash=?, must_change_password=0, password_expires_at=NULL WHERE id=?", (hashed, current["id"]))
                conn.execute("DELETE FROM account_sessions WHERE user_id=?", (current["id"],))
                self._audit(conn, current["id"], "change_password", current["id"])
        finally:
            try:
                self.logout(reauth_token)
            except AuthenticationError:
                pass

    def reset_password(self, token: str, user_id: str, new_password: str, admin_password: str) -> None:
        actor = self.authenticate(token, admin=True)
        if actor["id"] == user_id:
            raise AuthorizationError("请通过修改密码验证当前密码")
        reauth_token = self.login(actor["username"], admin_password)
        try:
            hashed = _password_hash(new_password)
            with closing(self._connect()) as conn, conn:
                conn.execute("BEGIN IMMEDIATE")
                current = self._session(conn, token, admin=True)
                reauthenticated = self._session(conn, reauth_token, admin=True)
                if current["id"] != reauthenticated["id"] or current["id"] != actor["id"]:
                    raise AuthenticationError(_LOGIN_ERROR)
                target = conn.execute("SELECT * FROM account_users WHERE id=?", (user_id,)).fetchone()
                if target is None:
                    raise ValueError("账户不存在")
                conn.execute("UPDATE account_users SET password_hash=?, must_change_password=1, password_expires_at=? WHERE id=?", (hashed, time.time() + _TEMP_PASSWORD_SECONDS, user_id))
                conn.execute("DELETE FROM account_sessions WHERE user_id=?", (user_id,))
                conn.execute("DELETE FROM account_login_attempts WHERE username=?", (target["username"],))
                self._audit(conn, current["id"], "reset_password", user_id)
        finally:
            try:
                self.logout(reauth_token)
            except AuthenticationError:
                pass

    def _update_user(self, token: str, user_id: str, *, role: str | None = None,
                     active: bool | None = None) -> None:
        if role is not None and role not in {"user", "admin"}:
            raise ValueError("未知账户角色")
        if active is not None and not isinstance(active, bool):
            raise ValueError("账户状态必须为布尔值")
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            actor = self._session(conn, token, admin=True)
            target = conn.execute("SELECT * FROM account_users WHERE id=?", (user_id,)).fetchone()
            if target is None:
                raise ValueError("账户不存在")
            new_role = role if role is not None else target["role"]
            new_active = int(active) if active is not None else target["active"]
            removes_admin = (target["role"] == "admin" and target["active"]
                             and not target["must_change_password"]
                             and (new_role != "admin" or not new_active))
            if removes_admin and conn.execute(
                "SELECT COUNT(*) FROM account_users WHERE role='admin' AND active=1 AND must_change_password=0"
            ).fetchone()[0] <= 1:
                raise AuthorizationError("不能停用或降权最后一名管理员")
            if new_role != target["role"] or new_active != target["active"]:
                conn.execute("UPDATE account_users SET role=?, active=? WHERE id=?", (new_role, new_active, user_id))
                conn.execute("DELETE FROM account_sessions WHERE user_id=?", (user_id,))
                action = f"change_user:role={target['role']}->{new_role},active={target['active']}->{new_active}"
                self._audit(conn, actor["id"], action, user_id)

    def set_role(self, token: str, user_id: str, role: str) -> None:
        self._update_user(token, user_id, role=role)

    def set_active(self, token: str, user_id: str, active: bool) -> None:
        self._update_user(token, user_id, active=active)
