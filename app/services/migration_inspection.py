"""Read-only inspection of separate SQLite snapshots; not a migration or release gate.

Two online backups are not a cross-database atomic snapshot.  A real migration
requires a maintenance window and a separately audited schema/data copier.
Never use the result as proof that old HTTP endpoints enforce ownership.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
import time
from contextlib import closing
from pathlib import Path
from typing import Any

_ACCOUNT_TABLES = {
    "account_users", "account_sessions", "account_login_attempts", "account_login_budget", "account_audit",
    "rule_versions", "rule_active_versions", "rule_candidates", "rule_candidate_reviews", "rule_candidate_details",
    "rule_submission_requests", "rule_validation_results", "rule_validation_jobs", "rule_full_regression_results",
    "rule_release_events", "rule_validation_control",
    # 施工卡-20261114-2：恢复代次账本两表（追加式；见 rule_validation_service）
    "rule_validation_recovery_log", "rule_evidence_generations",
}
_REVIEW_TABLES = {
    "reviews", "revision_budget", "revision_drafts", "selected_drafts", "impact_reports",
    "remediation_calls", "ai_finding_status", "llm_calls",
}
_ACCOUNT_USER_COLUMNS = {"id", "username", "display_name", "password_hash", "role", "active", "created_at",
                         "must_change_password", "password_expires_at"}
_REVIEW_COLUMNS = {"id", "created_at", "data"}


def _snapshot(source: Path, target: Path, deadline: float) -> None:
    def progress(_status: int, _remaining: int, _total: int) -> None:
        if time.monotonic() > deadline:
            raise TimeoutError("数据库快照超时")

    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=1)) as src:
        src.execute("PRAGMA trusted_schema=OFF")
        with closing(sqlite3.connect(target)) as dst:
            src.backup(dst, pages=256, progress=progress, sleep=0.05)


def _inspect(path: Path, *, account: bool, deadline: float) -> dict[str, Any]:
    if time.monotonic() > deadline:
        raise TimeoutError("数据库检查超时")
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as conn:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("PRAGMA trusted_schema=OFF")
        conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        objects = conn.execute("SELECT type, name, sql FROM sqlite_master").fetchall()
        tables = {name for kind, name, _ in objects if kind == "table"}
        allowed = _ACCOUNT_TABLES if account else _REVIEW_TABLES
        unexpected_count = len(tables - allowed)
        view_count = sum(kind == "view" for kind, _, _ in objects)
        virtual_tables = {name for kind, name, sql in objects if kind == "table" and
                          isinstance(sql, str) and sql.lstrip().upper().startswith("CREATE VIRTUAL TABLE")}
        if virtual_tables & allowed:
            raise ValueError("核心表不能是虚拟表")
        if conn.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise ValueError("数据库完整性检查失败")
        required = {"account_users", "account_sessions"} if account else {"reviews"}
        missing = sorted(required - tables)
        result: dict[str, Any] = {"tables": sorted(tables & allowed), "unexpected_table_count": unexpected_count,
                                  "view_count": view_count, "missing_tables": missing, "missing_columns": []}
        if account and "account_users" in tables:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(account_users)")}
            result["missing_columns"] = sorted(_ACCOUNT_USER_COLUMNS - cols)
            if "rule_validation_jobs" in tables:
                result["running_rule_jobs"] = conn.execute(
                    "SELECT COUNT(*) FROM rule_validation_jobs WHERE status='running'"
                ).fetchone()[0]
        if not account and "reviews" in tables:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(reviews)")}
            result["missing_columns"] = sorted(_REVIEW_COLUMNS - cols)
            if _REVIEW_COLUMNS.issubset(cols):
                result["review_count"] = conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0]
                result["invalid_review_json"] = conn.execute(
                    "SELECT COUNT(*) FROM reviews WHERE json_valid(data)=0"
                ).fetchone()[0]
                result["processing_reviews"] = conn.execute(
                    "SELECT COUNT(*) FROM reviews WHERE CASE WHEN json_valid(data) THEN json_extract(data,'$.status')='processing' ELSE 0 END"
                ).fetchone()[0]
                result["owned_reviews"] = (conn.execute("SELECT COUNT(*) FROM reviews WHERE owner_user_id IS NOT NULL").fetchone()[0]
                                           if "owner_user_id" in cols else 0)
        if time.monotonic() > deadline:
            raise TimeoutError("数据库检查超时")
        return result


def inspect_migration_sources(account_path: Path, review_path: Path, *, timeout_seconds: float = 15) -> dict[str, Any]:
    """Inspect temporary backups only; do not create a migrated destination.

    This creates temporary full copies with the OS temporary directory's ACL;
    deletion is not secure erasure and a crash may leave copies. The timeout is
    best effort, not an OS-enforced deadline. It does not certify source SQL,
    stop writers, migrate rows, or prove snapshots are contemporaneous.
    """
    paths = [Path(account_path), Path(review_path)]
    if any(not path.is_absolute() or not path.is_file() for path in paths):
        raise ValueError("源数据库必须是现有绝对路径")
    paths = [path.resolve() for path in paths]
    if paths[0] == paths[1] or os.path.samefile(*paths):
        raise ValueError("账户和合同源库必须不同")
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or not 0 < timeout_seconds <= 120:
        raise ValueError("快照时限无效")
    with tempfile.TemporaryDirectory(prefix="agent-t-migration-inspect-") as temp:
        deadline = time.monotonic() + timeout_seconds
        account_snapshot, review_snapshot = Path(temp) / "accounts.sqlite", Path(temp) / "reviews.sqlite"
        _snapshot(paths[0], account_snapshot, deadline)
        _snapshot(paths[1], review_snapshot, deadline)
        try:
            account = _inspect(account_snapshot, account=True, deadline=deadline)
            review = _inspect(review_snapshot, account=False, deadline=deadline)
        except sqlite3.OperationalError as exc:
            if time.monotonic() > deadline:
                raise TimeoutError("数据库检查超时") from exc
            raise
        return {"account": account, "review": review,
                "migration_ready": False,
                "limitation": ("会在系统临时目录生成包含敏感数据的完整副本；权限取决于目录设置，"
                               "异常退出可能残留，删除不等于安全擦除。时限为尽力控制，非强制中断。"
                               "未冻结两库、验证完整 schema 或执行迁移。")}
