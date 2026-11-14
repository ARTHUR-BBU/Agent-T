"""施工卡-20261114-2：恢复代次账本两表的建表、追加式保护、升级与迁移兼容。

背景：codex 实测 ALTER TABLE ADD COLUMN 会改写 sqlite_master 建表原文，
逐字节迁移比对随之破坏——因此代次走独立追加式账本，不给既有表加列。
"""
from __future__ import annotations

import sqlite3
import uuid

import pytest

from app.services import rule_validation_service
from app.services.account_review_migration import create_migrated_copy
from app.services.account_store import AccountStore
from app.services.rule_validation_service import RuleValidationService

_RECOVERY_TABLES = ("rule_validation_recovery_log", "rule_evidence_generations")
_RECOVERY_TRIGGERS = (
    "recovery_log_no_update", "recovery_log_no_delete", "recovery_log_no_replace",
    "evidence_generation_no_update", "evidence_generation_no_delete", "evidence_generation_no_replace",
)
_PASSWORD = "testing-password-123"


@pytest.fixture
def accounts(tmp_path):
    accounts = AccountStore(tmp_path / "account.sqlite")
    accounts.bootstrap_admin("admin", "管理员", _PASSWORD)
    return accounts


def _objects(path) -> dict[tuple[str, str], str | None]:
    with sqlite3.connect(path) as conn:
        return {(kind, name): sql for kind, name, sql in conn.execute(
            "SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL")}


def _drop_recovery_tables(path) -> None:
    """把库退回 3af1403 时代的形态（只有验证三表，无恢复账本）。"""
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA foreign_keys=OFF")
        for name in _RECOVERY_TRIGGERS:
            conn.execute(f"DROP TRIGGER IF EXISTS {name}")
        for name in _RECOVERY_TABLES:
            conn.execute(f"DROP TABLE IF EXISTS {name}")


def test_fresh_init_creates_recovery_tables_and_triggers(accounts):
    RuleValidationService(accounts)
    with sqlite3.connect(accounts.path) as conn:
        tables = {name for kind, name in conn.execute(
            "SELECT type,name FROM sqlite_master WHERE type='table'")}
        triggers = {name for kind, name in conn.execute(
            "SELECT type,name FROM sqlite_master WHERE type='trigger'")}
    assert set(_RECOVERY_TABLES).issubset(tables)
    assert set(_RECOVERY_TRIGGERS).issubset(triggers)


def test_recovery_log_append_only(accounts):
    service = RuleValidationService(accounts)
    token = accounts.login("admin", _PASSWORD)
    actor_id = accounts.authenticate(token)["id"]
    row = (uuid.uuid4().hex, "unlock", actor_id, "expired_job", uuid.uuid4().hex,
           "{}", "vol-1:file-1", 1000.0)
    with sqlite3.connect(accounts.path) as conn:
        conn.execute("INSERT INTO rule_validation_recovery_log "
                     "(id,action,actor_id,incident_reason,last_job_id,evidence_json,"
                     "db_file_identity,created_at) VALUES (?,?,?,?,?,?,?,?)", row)
        assert service.unlock_generation(conn) == 1
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("UPDATE rule_validation_recovery_log SET incident_reason='x'")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("DELETE FROM rule_validation_recovery_log")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("INSERT INTO rule_validation_recovery_log "
                         "(id,action,actor_id,incident_reason,last_job_id,evidence_json,"
                         "db_file_identity,created_at) VALUES (?,?,?,?,?,?,?,?)", row)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO rule_validation_recovery_log "
                         "(id,action,actor_id,incident_reason,last_job_id,evidence_json,"
                         "db_file_identity,created_at) VALUES (?,?,?,?,?,?,?,?)",
                         (uuid.uuid4().hex, "bogus", actor_id, None, None, "{}", "x", 0))


def test_evidence_generation_append_only(accounts):
    RuleValidationService(accounts)
    row = (uuid.uuid4().hex, "validation", 1, 1000.0)
    with sqlite3.connect(accounts.path) as conn:
        conn.execute("INSERT INTO rule_evidence_generations "
                     "(evidence_id,evidence_kind,recovery_generation,created_at) "
                     "VALUES (?,?,?,?)", row)
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("UPDATE rule_evidence_generations SET recovery_generation=2")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("DELETE FROM rule_evidence_generations")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO rule_evidence_generations "
                         "(evidence_id,evidence_kind,recovery_generation,created_at) "
                         "VALUES (?,?,?,?)", (uuid.uuid4().hex, "bogus_kind", 1, 0))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO rule_evidence_generations "
                         "(evidence_id,evidence_kind,recovery_generation,created_at) "
                         "VALUES (?,?,?,?)", (uuid.uuid4().hex, "validation", 0, 0))


def test_legacy_upgrade_preserves_byte_contract(accounts, tmp_path):
    """旧库（无恢复账本）升级后：既有对象逐字节不动，且与全新库逐字节一致。"""
    upgraded_path = accounts.path
    RuleValidationService(accounts)
    before = {key: sql for key, sql in _objects(upgraded_path).items()
              if not (key[1] in _RECOVERY_TABLES or key[1] in _RECOVERY_TRIGGERS)}
    _drop_recovery_tables(upgraded_path)
    RuleValidationService(AccountStore(upgraded_path))  # 升级：重建账本两表
    after = {key: sql for key, sql in _objects(upgraded_path).items()
             if not (key[1] in _RECOVERY_TABLES or key[1] in _RECOVERY_TRIGGERS)}
    assert before == after  # 既有对象逐字节不动
    fresh = AccountStore(tmp_path / "fresh.sqlite")
    RuleValidationService(fresh)
    fresh_objects = {key: sql for key, sql in _objects(fresh.path).items()
                     if not (key[1] in _RECOVERY_TABLES or key[1] in _RECOVERY_TRIGGERS)}
    assert after == fresh_objects  # 升级库与全新库逐字节一致


def test_repeated_upgrade_idempotent(accounts):
    RuleValidationService(accounts)
    snapshot = _objects(accounts.path)
    RuleValidationService(accounts)
    RuleValidationService(AccountStore(accounts.path))
    assert _objects(accounts.path) == snapshot
    with sqlite3.connect(accounts.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM rule_validation_control WHERE id=1").fetchone()[0] == 1


def test_constructor_failure_rolls_back_upgrade(accounts, monkeypatch):
    """升级中途失败：账本两表不存在，既有对象与控制行原样保留。"""
    RuleValidationService(accounts)
    _drop_recovery_tables(accounts.path)
    original_split = rule_validation_service._split_sqlite_script

    def poisoned_split(script):
        statements = original_split(script)
        if script is rule_validation_service._RECOVERY_SCHEMA_SQL:
            statements.append("INSERT INTO rule_validation_recovery_log "
                              "(id,action,actor_id,incident_reason,last_job_id,evidence_json,"
                              "db_file_identity,created_at) VALUES ('poison','bogus',NULL,"
                              "NULL,NULL,'{}','x',0)")
        return statements

    monkeypatch.setattr(rule_validation_service, "_split_sqlite_script", poisoned_split)
    with pytest.raises(sqlite3.IntegrityError):
        RuleValidationService(AccountStore(accounts.path))
    objects = _objects(accounts.path)
    assert all(("table", name) not in objects for name in _RECOVERY_TABLES)
    assert ("table", "rule_validation_control") in objects
    with sqlite3.connect(accounts.path) as conn:
        assert conn.execute("SELECT blocked FROM rule_validation_control WHERE id=1").fetchone()[0] == 0


def test_migration_rejects_recovery_log_without_validation_tables(tmp_path):
    from app.services.store import ReviewStore

    account_path, review_path = tmp_path / "account.sqlite", tmp_path / "review.sqlite"
    accounts = AccountStore(account_path)
    accounts.bootstrap_admin("admin", "管理员", _PASSWORD)
    RuleValidationService(accounts)
    ReviewStore(str(review_path)).create(filename="synthetic.txt", status="done", text="内容")
    with sqlite3.connect(account_path) as conn:
        conn.execute("PRAGMA foreign_keys=OFF")
        # 只删验证三表及其触发器；账本两表及其触发器保留——
        # _check_schema 要求在场表的触发器齐全，缺了会先撞「缺少必要的
        # 索引或触发器」，轮不到恢复账本闸门
        for name in (
            "validation_control_no_delete", "validation_control_no_online_reset",
            "one_running_rule_validation", "validation_job_no_delete",
            "validation_job_no_replace", "validation_job_valid_insert",
            "validation_job_transition", "validation_no_update", "validation_no_delete",
            "validation_no_replace",
        ):
            conn.execute(f"DROP TRIGGER IF EXISTS {name}")
        for name in ("rule_validation_results", "rule_validation_jobs", "rule_validation_control"):
            conn.execute(f"DROP TABLE IF EXISTS {name}")
    with pytest.raises(ValueError, match="恢复账本缺少规则验证三表"):
        create_migrated_copy(account_path, review_path, tmp_path / "out.sqlite",
                             maintenance_confirmed=True)


def test_migration_accepts_current_schema_with_recovery_ledger(tmp_path):
    from app.services.store import ReviewStore

    account_path, review_path = tmp_path / "account.sqlite", tmp_path / "review.sqlite"
    accounts = AccountStore(account_path)
    accounts.bootstrap_admin("admin", "管理员", _PASSWORD)
    RuleValidationService(accounts)
    ReviewStore(str(review_path)).create(filename="synthetic.txt", status="done", text="内容")
    result = create_migrated_copy(account_path, review_path, tmp_path / "out.sqlite",
                                  maintenance_confirmed=True)
    assert result["created"] is True
