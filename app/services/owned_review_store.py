"""Internal same-database ownership primitives; not a protected application mode.

The existing contract routes and background workers do not use this class.  Do
not enable multi-user contract access until every entry point, related table,
long-running task, session transport, and migration has been secured and tested.
"""
from __future__ import annotations

import json
import os
import sys
import time
import uuid
from copy import deepcopy
from contextlib import closing
from pathlib import Path
from typing import Any, Callable

from app.services.account_store import AccountStore
from app.services import store as store_module
from app.services.store import ReviewStore

_RELATED_TABLES = (
    "ai_finding_status", "llm_calls", "remediation_calls", "impact_reports",
    "selected_drafts", "revision_drafts", "revision_budget",
)
_COVERED_HOOKS = (
    ("app.services.revision", "_purge_hook"),
    ("app.services.impact", "_purge_hook"),
    ("app.services.remediation_budget", "_purge"),
)


class OwnedReviewStore:
    """Bind authenticated owners and reviews in one SQLite transaction domain."""

    def __init__(self, accounts: AccountStore, reviews: ReviewStore) -> None:
        account_path = Path(accounts.path).resolve()
        review_path = Path(reviews._path).resolve()
        if account_path != review_path and not (
            account_path.exists() and review_path.exists() and os.path.samefile(account_path, review_path)
        ):
            raise ValueError("账户与合同必须位于同一数据库")
        self.accounts = accounts
        self.reviews = reviews
        with closing(accounts._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(reviews)")}
            if not {"id", "created_at", "data"}.issubset(columns):
                raise RuntimeError("合同数据库结构无效")
            if "owner_user_id" not in columns:
                conn.execute("ALTER TABLE reviews ADD COLUMN owner_user_id TEXT")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_reviews_owner_created ON reviews(owner_user_id, created_at DESC)")

    def _visible(self, created_at: float) -> bool:
        return not self.reviews._is_expired(created_at)

    @staticmethod
    def _require_known_hooks() -> None:
        covered = {getattr(sys.modules[name], attr, None) for name, attr in _COVERED_HOOKS if name in sys.modules}
        if any(hook not in covered for hook in tuple(store_module.PURGE_HOOKS)):
            raise RuntimeError("存在未纳入事务清理的扩展，不能删除过期合同")

    def _remove_expired(self, conn: Any, review_id: str) -> None:
        # These tables have review_id in the current schema.  Failure aborts
        # the transaction, so the review remains available for later cleanup.
        self._require_known_hooks()
        existing = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in _RELATED_TABLES:
            if table in existing:
                conn.execute(f'DELETE FROM "{table}" WHERE review_id=?', (review_id,))
        conn.execute("DELETE FROM reviews WHERE id=?", (review_id,))

    def _purge_expired(self, conn: Any) -> None:
        if self.reviews.ttl_seconds <= 0:
            return
        cutoff = time.time() - self.reviews.ttl_seconds
        rows = conn.execute("SELECT id FROM reviews WHERE created_at < ? ORDER BY created_at LIMIT 100", (cutoff,)).fetchall()
        for row in rows:
            self._remove_expired(conn, row["id"])
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='llm_calls'").fetchone():
            orphans = conn.execute(
                "SELECT c.call_id FROM llm_calls c "
                "WHERE c.created_at < ? AND (c.review_id IS NULL OR NOT EXISTS "
                "(SELECT 1 FROM reviews r WHERE r.id=c.review_id)) ORDER BY c.created_at LIMIT 100", (cutoff,),
            ).fetchall()
            if orphans:
                self._require_known_hooks()
                conn.executemany("DELETE FROM llm_calls WHERE call_id=?", [(row["call_id"],) for row in orphans])

    def create(self, token: str, **fields: Any) -> str:
        if {"id", "owner_user_id"} & fields.keys():
            raise ValueError("合同编号和所有者由服务器确定")
        rid = uuid.uuid4().hex[:12]
        data = json.dumps({"id": rid, **fields}, ensure_ascii=False)
        with closing(self.accounts._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            actor = self.accounts._session(conn, token)
            self._purge_expired(conn)
            self.accounts._session(conn, token)
            conn.execute("INSERT INTO reviews (id, created_at, data, owner_user_id) VALUES (?,?,?,?)",
                         (rid, time.time(), data, actor["id"]))
        return rid

    def get(self, token: str, review_id: str) -> dict[str, Any] | None:
        with closing(self.accounts._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            actor = self.accounts._session(conn, token)
            row = conn.execute("SELECT created_at, data FROM reviews WHERE id=? AND owner_user_id=?",
                               (review_id, actor["id"])).fetchone()
            if row is None:
                return None
            if not self._visible(row["created_at"]):
                self._remove_expired(conn, review_id)
                return None
            return json.loads(row["data"])

    def list_reviews(self, token: str, *, limit: int = 50) -> list[dict[str, Any]]:
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 50:
            raise ValueError("列表数量无效")
        with closing(self.accounts._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            actor = self.accounts._session(conn, token)
            cutoff = time.time() - self.reviews.ttl_seconds if self.reviews.ttl_seconds > 0 else None
            rows = conn.execute(
                "SELECT id, created_at, data FROM reviews WHERE owner_user_id=? AND (? IS NULL OR created_at>=?) "
                "ORDER BY created_at DESC, id DESC LIMIT ?", (actor["id"], cutoff, cutoff, limit),
            ).fetchall()
            result = []
            for row in rows:
                data = json.loads(row["data"])
                result.append({"id": row["id"], "created_at": row["created_at"],
                               "filename": data.get("filename") or "", "category": data.get("category") or "",
                               "category_label": data.get("category_label") or "", "status": data.get("status") or "pending"})
            return result

    def mutate(self, token: str, review_id: str, change: Callable[[dict[str, Any]], None]) -> dict[str, Any] | None:
        with closing(self.accounts._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            actor = self.accounts._session(conn, token)
            row = conn.execute("SELECT created_at, data FROM reviews WHERE id=? AND owner_user_id=?",
                               (review_id, actor["id"])).fetchone()
            if row is None:
                return None
            if not self._visible(row["created_at"]):
                self._remove_expired(conn, review_id)
                return None
            data = json.loads(row["data"])
            protected_keys = ("input_sha256", "input_stance", "input_kind", "require_precheck", "filename", "category", "rule_provenance")
            protected = {key: deepcopy(data.get(key)) for key in protected_keys} if "input_sha256" in data else None
            change(data)
            if data.get("id") != review_id or "owner_user_id" in data:
                raise ValueError("不能更改合同编号或所有者")
            if ((protected is not None and any(data.get(key) != value for key, value in protected.items()))
                    or (protected is None and ("input_sha256" in data or "input_stance" in data))):
                raise ValueError("不能更改已绑定的合同输入或规则版本")
            self.accounts._session(conn, token)
            if not self._visible(row["created_at"]):
                self._remove_expired(conn, review_id)
                return None
            conn.execute("UPDATE reviews SET data=? WHERE id=? AND owner_user_id=?",
                         (json.dumps(data, ensure_ascii=False), review_id, actor["id"]))
            return data
