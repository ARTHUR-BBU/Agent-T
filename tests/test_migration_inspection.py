"""Inspection uses isolated SQLite backups and never claims migration readiness."""
import sqlite3
from pathlib import Path

import pytest

from app.services.account_store import AccountStore
from app.services.migration_inspection import inspect_migration_sources
from app.services.store import ReviewStore


def test_inspection_sees_committed_wal_and_leaves_source_records_unchanged(tmp_path):
    accounts_path = tmp_path / "accounts.sqlite"
    reviews_path = tmp_path / "reviews.sqlite"
    accounts = AccountStore(accounts_path)
    accounts.bootstrap_admin("admin", "管理员", "testing-password-123")
    reviews = ReviewStore(str(reviews_path))
    rid = reviews.create(filename="保密合同", status="processing")
    # Keep a WAL writer open so committed content can remain in the WAL.
    with sqlite3.connect(reviews_path) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("CREATE TABLE unexpected_extension (id TEXT)")
        writer.execute("INSERT INTO unexpected_extension VALUES ('not reported')")
        writer.execute('CREATE TABLE "sensitive-object-name" (id TEXT)')
        writer.commit()
        report = inspect_migration_sources(accounts_path, reviews_path)
        assert writer.execute("SELECT COUNT(*) FROM reviews WHERE id=?", (rid,)).fetchone()[0] == 1
    assert report["migration_ready"] is False
    assert "临时目录" in report["limitation"] and "异常退出可能残留" in report["limitation"]
    assert report["review"]["review_count"] == 1
    assert report["review"]["processing_reviews"] == 1
    assert report["review"]["unexpected_table_count"] == 2
    assert "保密合同" not in str(report) and "not reported" not in str(report)
    assert "sensitive-object-name" not in str(report)
    assert report["account"]["missing_columns"] == []


def test_inspection_identifies_legacy_and_invalid_rows(tmp_path):
    account_path, review_path = tmp_path / "a.sqlite", tmp_path / "r.sqlite"
    with sqlite3.connect(account_path) as conn:
        conn.execute("CREATE TABLE account_users (id TEXT PRIMARY KEY, username TEXT)")
        conn.execute("CREATE TABLE account_sessions (token_hash TEXT)")
    with sqlite3.connect(review_path) as conn:
        conn.execute("CREATE TABLE reviews (id TEXT, created_at REAL, data TEXT, owner_user_id TEXT)")
        conn.execute("INSERT INTO reviews VALUES ('a',1,'not-json',NULL)")
        conn.execute("INSERT INTO reviews VALUES ('b',2,'{}','unknown-owner')")
    report = inspect_migration_sources(account_path, review_path)
    assert "must_change_password" in report["account"]["missing_columns"]
    assert report["review"]["invalid_review_json"] == 1
    assert report["review"]["owned_reviews"] == 1
    assert report["migration_ready"] is False


def test_inspection_rejects_same_database_and_missing_sources(tmp_path):
    path = tmp_path / "a.sqlite"
    sqlite3.connect(path).close()
    with pytest.raises(ValueError, match="必须不同"):
        inspect_migration_sources(path, path)
    with pytest.raises(ValueError, match="现有绝对路径"):
        inspect_migration_sources(path, tmp_path / "missing.sqlite")
    with pytest.raises(ValueError, match="现有绝对路径"):
        inspect_migration_sources(Path("relative.sqlite"), path)


def test_inspection_detects_expired_deadline(tmp_path):
    from app.services.migration_inspection import _inspect

    path = tmp_path / "snapshot.sqlite"
    sqlite3.connect(path).close()
    with pytest.raises(TimeoutError, match="超时"):
        _inspect(path, account=True, deadline=0)


def test_virtual_core_review_table_is_rejected(tmp_path):
    account_path, review_path = tmp_path / "a.sqlite", tmp_path / "r.sqlite"
    with sqlite3.connect(account_path) as conn:
        conn.execute("CREATE TABLE account_users (id TEXT)")
        conn.execute("CREATE TABLE account_sessions (token_hash TEXT)")
    with sqlite3.connect(review_path) as conn:
        conn.execute("CREATE VIRTUAL TABLE reviews USING fts5(data)")
    with pytest.raises(ValueError, match="虚拟表"):
        inspect_migration_sources(account_path, review_path)


def test_inspection_counts_tampered_reserved_table_name(tmp_path):
    account_path, review_path = tmp_path / "a.sqlite", tmp_path / "r.sqlite"
    with sqlite3.connect(account_path) as conn:
        conn.execute("CREATE TABLE account_users (id TEXT)")
        conn.execute("CREATE TABLE account_sessions (token_hash TEXT)")
    with sqlite3.connect(review_path) as conn:
        conn.execute("CREATE TABLE reviews (id TEXT, created_at REAL, data TEXT)")
        conn.execute("CREATE TABLE hidden (id TEXT)")
        conn.execute("PRAGMA writable_schema=ON")
        conn.execute("UPDATE sqlite_master SET name='sqlite_shadow',tbl_name='sqlite_shadow', "
                     "sql='CREATE TABLE sqlite_shadow (id TEXT)' WHERE name='hidden'")
        conn.execute("PRAGMA writable_schema=OFF")
    report = inspect_migration_sources(account_path, review_path)
    assert report["review"]["unexpected_table_count"] == 1
    assert "sqlite_shadow" not in str(report)
