"""Database-backed immutable rule versions and effective-version pointers.

Initialization is an explicit administrator action. Publication is deliberately
not exposed until validated candidates and approvals share this transaction.
"""
from __future__ import annotations

import hashlib
import time
import uuid
from contextlib import closing
from dataclasses import dataclass
from typing import Any

from app.services.account_store import AccountStore
from app.services.rule_snapshot import RuleSnapshot, capture_rule_snapshot


@dataclass(frozen=True)
class CapturedRuleVersion:
    """One database read binds exact rule bytes to their version and generation."""

    snapshot: RuleSnapshot
    version_id: str
    generation: int
    sha256: str


class RuleVersionStore:
    def __init__(self, accounts: AccountStore) -> None:
        self.accounts = accounts
        with closing(accounts._connect()) as conn, conn:
            existing = conn.execute("PRAGMA table_info(rule_active_versions)").fetchall()
            if existing and "generation" not in {row["name"] for row in existing}:
                raise RuntimeError("旧版规则活动指针需要安全迁移；请使用新的试验库")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS rule_versions (
                    id TEXT PRIMARY KEY, category TEXT NOT NULL, sha256 TEXT NOT NULL,
                    content BLOB NOT NULL, actor_id TEXT NOT NULL REFERENCES account_users(id),
                    created_at REAL NOT NULL, UNIQUE(category, sha256)
                );
                CREATE TABLE IF NOT EXISTS rule_active_versions (
                    category TEXT PRIMARY KEY, version_id TEXT NOT NULL REFERENCES rule_versions(id),
                    generation INTEGER NOT NULL CHECK(generation >= 1)
                );
                CREATE TRIGGER IF NOT EXISTS active_version_no_replace BEFORE INSERT ON rule_active_versions
                WHEN EXISTS (SELECT 1 FROM rule_active_versions WHERE category=NEW.category)
                BEGIN SELECT RAISE(ABORT, 'active version cannot be replaced'); END;
                CREATE TRIGGER IF NOT EXISTS active_version_no_delete BEFORE DELETE ON rule_active_versions
                BEGIN SELECT RAISE(ABORT, 'active version cannot be deleted'); END;
                CREATE TRIGGER IF NOT EXISTS active_version_generation BEFORE UPDATE ON rule_active_versions
                WHEN NEW.category != OLD.category OR NEW.generation != OLD.generation + 1
                BEGIN SELECT RAISE(ABORT, 'active generation must advance'); END;
                CREATE TRIGGER IF NOT EXISTS rule_versions_no_update BEFORE UPDATE ON rule_versions
                BEGIN SELECT RAISE(ABORT, 'versions are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS rule_versions_no_delete BEFORE DELETE ON rule_versions
                BEGIN SELECT RAISE(ABORT, 'versions are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS rule_versions_no_replace BEFORE INSERT ON rule_versions
                WHEN EXISTS (SELECT 1 FROM rule_versions WHERE id=NEW.id OR
                             (category=NEW.category AND sha256=NEW.sha256))
                BEGIN SELECT RAISE(ABORT, 'versions are append-only'); END;
            """)

    def initialize(self, token: str, category: str, content: bytes) -> dict[str, Any]:
        self.accounts.authenticate(token, admin=True)
        if not isinstance(content, bytes) or not content or len(content) > 2_000_000:
            raise ValueError("规则内容无效或过大")
        digest = hashlib.sha256(content).hexdigest()
        RuleSnapshot(category=category, config_bytes=content, content_version=digest[:12])
        if content != capture_rule_snapshot(category).config_bytes:
            raise ValueError("初始规则必须与当前文件一致")
        with closing(self.accounts._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            actor = self.accounts._session(conn, token, admin=True)
            if conn.execute("SELECT 1 FROM rule_active_versions WHERE category=?", (category,)).fetchone():
                raise ValueError("品类规则版本已初始化")
            version_id, now = uuid.uuid4().hex, time.time()
            # 施工卡-20261114-2：INSERT 一律写明确列名，不再依赖列序
            conn.execute("INSERT INTO rule_versions (id,category,sha256,content,actor_id,created_at) "
                         "VALUES (?,?,?,?,?,?)", (
                version_id, category, digest, content, actor["id"], now,
            ))
            conn.execute("INSERT INTO rule_active_versions (category,version_id,generation) VALUES (?,?,1)",
                         (category, version_id))
            self.accounts._audit(conn, actor["id"], "initialize_rule_version", version_id)
            return {"id": version_id, "category": category, "sha256": digest,
                    "actor_id": actor["id"], "created_at": now}

    def current(self, category: str) -> RuleSnapshot:
        return self.capture_current(category).snapshot

    def capture_current(self, category: str) -> CapturedRuleVersion:
        """Capture bytes and pointer metadata together; never reread the pointer to label them."""
        with closing(self.accounts._connect()) as conn:
            row = conn.execute(
                "SELECT v.*, a.generation AS active_generation FROM rule_active_versions a JOIN rule_versions v ON v.id=a.version_id "
                "WHERE a.category=? AND v.category=a.category", (category,),
            ).fetchone()
            if row is None:
                raise ValueError("品类规则版本未初始化")
            raw = row["content"]
            digest = hashlib.sha256(raw).hexdigest()
            if digest != row["sha256"]:
                raise ValueError("规则版本内容损坏")
            snapshot = RuleSnapshot(category=category, config_bytes=raw, content_version=digest[:12])
            return CapturedRuleVersion(snapshot=snapshot, version_id=row["id"],
                                       generation=row["active_generation"], sha256=digest)
