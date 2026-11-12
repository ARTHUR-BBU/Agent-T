"""Experimental offline migration tests use synthetic, isolated databases only."""
import hashlib
import sqlite3

import pytest

from app.services.account_store import AccountStore, AuthenticationError
from app.services.account_review_migration import _check_schema, _objects, create_migrated_copy
from app.services.rule_snapshot import capture_rule_snapshot
from app.services.rule_versions import RuleVersionStore
from app.services.store import ReviewStore


@pytest.fixture
def source_databases(tmp_path):
    account_path, review_path = tmp_path / "account.sqlite", tmp_path / "review.sqlite"
    accounts = AccountStore(account_path)
    accounts.bootstrap_admin("admin", "管理员", "testing-password-123")
    token = accounts.login("admin", "testing-password-123")
    versions = RuleVersionStore(accounts)
    original = capture_rule_snapshot("procurement").config_bytes
    version = versions.initialize(token, "procurement", original)
    reviews = ReviewStore(str(review_path))
    rid = reviews.create(filename="synthetic.txt", status="done", text="synthetic content")
    return account_path, review_path, token, rid, version, original


def test_migration_preserves_data_rules_and_rowids_but_invalidates_sessions(source_databases, tmp_path):
    from app.services.impact import _SELECTED_DDL

    account_path, review_path, token, rid, version, original = source_databases
    target = tmp_path / "new.sqlite"
    with sqlite3.connect(account_path) as conn:
        source_audit = conn.execute("SELECT rowid,id,action FROM account_audit ORDER BY rowid").fetchall()
    with sqlite3.connect(review_path) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute(_SELECTED_DDL)  # A lazily created empty table need not have a draft table.
        writer.execute("UPDATE reviews SET data=json_set(data,'$.test_marker','committed WAL') WHERE id=?", (rid,))
        writer.commit()
        result = create_migrated_copy(account_path, review_path, target, maintenance_confirmed=True)
    assert result == {"created": True, "database_filename": "agent-t.sqlite", "old_sessions_copied": False, "legacy_reviews_owned": False,
                      "maintenance_enforced": False, "contract_access_integrated": False}
    with sqlite3.connect(target / "agent-t.sqlite") as conn:
        assert conn.execute("SELECT owner_user_id FROM reviews WHERE id=?", (rid,)).fetchone() == (None,)
        assert conn.execute("SELECT json_extract(data,'$.test_marker') FROM reviews WHERE id=?", (rid,)).fetchone() == ("committed WAL",)
        assert conn.execute("SELECT content,sha256 FROM rule_versions WHERE id=?", (version["id"],)).fetchone() == (original, hashlib.sha256(original).hexdigest())
        assert conn.execute("SELECT rowid,id,action FROM account_audit ORDER BY rowid").fetchall() == source_audit
        assert conn.execute("SELECT COUNT(*) FROM account_sessions").fetchone()[0] == 0
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("UPDATE rule_versions SET content=?", (b"tamper",))
    with pytest.raises(AuthenticationError):
        AccountStore(target / "agent-t.sqlite").authenticate(token)
    assert AccountStore(account_path).authenticate(token)["username"] == "admin"


def test_schema_verification_rejects_missing_triggers_and_sqlite_x_names():
    with sqlite3.connect(":memory:") as source, sqlite3.connect(":memory:") as reference:
        for conn in (source, reference):
            conn.execute("CREATE TABLE item (id TEXT)")
        reference.execute("CREATE TRIGGER protect BEFORE DELETE ON item BEGIN SELECT RAISE(ABORT,'protected'); END")
        with pytest.raises(ValueError, match="缺少必要"):
            _check_schema(source, reference, {"item"}, {"item"})
        source.execute("CREATE TABLE sqliteXsecret (id TEXT)")
        assert ("table", "sqliteXsecret") in _objects(source)
        with pytest.raises(ValueError, match="未知表"):
            _check_schema(source, reference, {"item"}, {"item"})


def test_schema_verification_does_not_hide_reserved_name_tampering(tmp_path):
    source_path = tmp_path / "tampered.sqlite"
    with sqlite3.connect(source_path) as conn:
        conn.execute("CREATE TABLE item (id TEXT)")
        conn.execute("CREATE TABLE hidden (id TEXT)")
        conn.execute("INSERT INTO hidden VALUES ('valuable')")
        conn.execute("PRAGMA writable_schema=ON")
        conn.execute("UPDATE sqlite_master SET name='sqlite_shadow',tbl_name='sqlite_shadow', "
                     "sql='CREATE TABLE sqlite_shadow (id TEXT)' WHERE name='hidden'")
        conn.execute("PRAGMA writable_schema=OFF")
    with sqlite3.connect(source_path) as source, sqlite3.connect(":memory:") as reference:
        reference.execute("CREATE TABLE item (id TEXT)")
        assert ("table", "sqlite_shadow") in _objects(source)
        with pytest.raises(ValueError, match="未知表"):
            _check_schema(source, reference, {"item"}, {"item"})


def test_sqlite_statistics_are_explicitly_rejected_instead_of_silently_dropped():
    with sqlite3.connect(":memory:") as source, sqlite3.connect(":memory:") as reference:
        for conn in (source, reference):
            conn.execute("CREATE TABLE item (id TEXT PRIMARY KEY)")
        source.execute("INSERT INTO item VALUES ('a')")
        source.execute("ANALYZE")
        assert ("table", "sqlite_stat1") in _objects(source)
        with pytest.raises(ValueError, match="未知表"):
            _check_schema(source, reference, {"item"}, {"item"})


def test_existing_target_sidecars_and_same_source_are_rejected(source_databases, tmp_path):
    account, review, *_ = source_databases
    target = tmp_path / "new.sqlite"
    target.write_bytes(b"keep")
    with pytest.raises(FileExistsError):
        create_migrated_copy(account, review, target, maintenance_confirmed=True)
    assert target.read_bytes() == b"keep"
    target.unlink()
    target.mkdir()
    (target / "agent-t.sqlite-wal").write_bytes(b"keep")
    with pytest.raises(FileExistsError):
        create_migrated_copy(account, review, target, maintenance_confirmed=True)
    with pytest.raises(ValueError, match="必须不同"):
        create_migrated_copy(account, account, tmp_path / "different.sqlite", maintenance_confirmed=True)
    with pytest.raises(ValueError, match="维护窗口"):
        create_migrated_copy(account, review, tmp_path / "different.sqlite", maintenance_confirmed=False)


def test_concurrent_target_creation_is_never_overwritten(source_databases, tmp_path, monkeypatch):
    from app.services import account_review_migration as migration

    account, review, *_ = source_databases
    target = tmp_path / "new.sqlite"
    real_publish = migration._publish_directory

    def racing_publish(source, destination):
        target.mkdir()
        (target / "agent-t.sqlite-wal").write_bytes(b"other-process-file")
        return real_publish(source, destination)

    monkeypatch.setattr(migration, "_publish_directory", racing_publish)
    with pytest.raises(FileExistsError):
        create_migrated_copy(account, review, target, maintenance_confirmed=True)
    assert (target / "agent-t.sqlite-wal").read_bytes() == b"other-process-file"


def test_unexpected_staging_sidecar_blocks_publication(source_databases, tmp_path, monkeypatch):
    from app.services import account_review_migration as migration

    account, review, *_ = source_databases
    target = tmp_path / "new-bundle"
    real_publish = migration._publish_directory

    def inject_sidecar(source, destination):
        (source / "agent-t.sqlite-wal").write_bytes(b"unexpected")
        return real_publish(source, destination)

    monkeypatch.setattr(migration, "_publish_directory", inject_sidecar)
    with pytest.raises(ValueError, match="暂存目录包含未预期文件"):
        create_migrated_copy(account, review, target, maintenance_confirmed=True)
    assert not target.exists()


def test_copy_holds_both_sqlite_write_locks_until_publication(source_databases, tmp_path, monkeypatch):
    from app.services import account_review_migration as migration

    account, review, *_ = source_databases
    real_snapshot = migration._snapshot
    real_publish = migration._publish_directory
    observations = []

    def assert_locked():
        for path in (account, review):
            with sqlite3.connect(path, timeout=0.05) as probe:
                with pytest.raises(sqlite3.OperationalError, match="locked"):
                    probe.execute("BEGIN IMMEDIATE")
            observations.append(path)

    def snapshot_while_locked(source, target, deadline):
        assert_locked()
        return real_snapshot(source, target, deadline)

    def publish_while_locked(source, target):
        assert_locked()
        return real_publish(source, target)

    monkeypatch.setattr(migration, "_snapshot", snapshot_while_locked)
    monkeypatch.setattr(migration, "_publish_directory", publish_while_locked)
    target = tmp_path / "locked-copy.sqlite"
    create_migrated_copy(account, review, target, maintenance_confirmed=True)
    assert observations == [account, review, account, review, account, review]
    # The lock ends after copying; this is why old processes must remain stopped.
    with sqlite3.connect(review, timeout=0.2) as conn:
        conn.execute("BEGIN IMMEDIATE")


def test_busy_second_source_releases_first_lock_without_creating_target(source_databases, tmp_path):
    account, review, *_ = source_databases
    target = tmp_path / "not-created.sqlite"
    with sqlite3.connect(review) as held:
        held.execute("BEGIN IMMEDIATE")
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            create_migrated_copy(account, review, target, maintenance_confirmed=True)
        with sqlite3.connect(account, timeout=0.2) as probe:
            probe.execute("BEGIN IMMEDIATE")
    assert not target.exists()


def test_failed_backup_releases_both_locks(source_databases, tmp_path, monkeypatch):
    from app.services import account_review_migration as migration

    account, review, *_ = source_databases

    def fail(*args, **kwargs):
        raise RuntimeError("synthetic backup failure")

    monkeypatch.setattr(migration, "_snapshot", fail)
    target = tmp_path / "not-created.sqlite"
    with pytest.raises(RuntimeError, match="synthetic backup failure"):
        create_migrated_copy(account, review, target, maintenance_confirmed=True)
    assert not target.exists()
    for path in (account, review):
        with sqlite3.connect(path, timeout=0.2) as conn:
            conn.execute("BEGIN IMMEDIATE")


def test_copy_succeeds_with_rollback_journals(source_databases, tmp_path):
    import gc

    account, review, _, rid, *_ = source_databases
    gc.collect()  # sqlite connection context managers commit but do not close handles.
    for path in (account, review):
        with sqlite3.connect(path) as conn:
            assert conn.execute("PRAGMA journal_mode=DELETE").fetchone()[0].lower() == "delete"
    target = tmp_path / "rollback-mode-copy.sqlite"
    create_migrated_copy(account, review, target, maintenance_confirmed=True)
    with sqlite3.connect(target / "agent-t.sqlite") as conn:
        assert conn.execute("SELECT owner_user_id FROM reviews WHERE id=?", (rid,)).fetchone() == (None,)


def test_orphan_related_data_rejects_migration_without_creating_target(source_databases, tmp_path):
    account, review, *_ = source_databases
    with sqlite3.connect(review) as conn:
        conn.execute("CREATE TABLE ai_finding_status (review_id TEXT NOT NULL, finding_key TEXT NOT NULL,status TEXT NOT NULL, updated_at REAL NOT NULL,PRIMARY KEY (review_id, finding_key))")
        conn.execute("INSERT INTO ai_finding_status VALUES ('missing','finding','viewed',1)")
    target = tmp_path / "new.sqlite"
    with pytest.raises(ValueError, match="孤儿"):
        create_migrated_copy(account, review, target, maintenance_confirmed=True)
    assert not target.exists()


def test_migration_preserves_candidate_validation_review_and_guards(source_databases, tmp_path, monkeypatch):
    import uuid
    from app.services import rule_review_ledger, rule_validation_service
    from app.services.rule_candidate_validation import GoldCheckResult
    from app.services.rule_review_ledger import RuleReviewLedger
    from app.services.rule_submission import RuleSubmissionService
    from app.services.rule_validation_service import RuleValidationService

    account, review, token, _, version, _ = source_databases
    accounts = AccountStore(account)
    monkeypatch.setattr(rule_review_ledger, "current_input_manifest", lambda snapshot: "a" * 64)

    def runner(snapshot):
        return GoldCheckResult(category=snapshot.category, candidate_sha256=hashlib.sha256(snapshot.config_bytes).hexdigest(),
                               input_manifest_sha256="a" * 64, suite="synthetic", passed=True,
                               passed_tests=1, collected_tests=1, skipped_tests=0, failed_test_ids=(),
                               exit_code=0, output_sha256="b" * 64)

    monkeypatch.setattr(rule_validation_service, "run_gold_checks", runner)
    draft = {"category": "procurement", "operation": "add", "item_id": "custom_delivery",
             "name": "交付清单", "description": "检查交付清单", "reason": "明确范围",
             "segment": "B", "literal": "交付清单", "positive": "按照交付清单验收",
             "negative": "进行验收", "base_sha256": version["sha256"]}
    candidate = RuleSubmissionService(accounts).submit(token, draft, expected_generation=1, request_id=uuid.uuid4().hex)
    validation = RuleValidationService(accounts).validate(token, candidate["id"])
    decision = RuleReviewLedger(accounts, versioned=True).review(token, candidate["id"], candidate["sha256"],
                                                                  "approve", "通过", expected_validation_id=validation["id"])
    with sqlite3.connect(account) as conn:
        # Simulate retained history with gaps; restore identical trusted triggers afterwards.
        for table, new_rowid in (("rule_validation_jobs", 71), ("rule_validation_results", 93),
                                 ("rule_candidate_reviews", 117)):
            triggers = conn.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name=?", (table,)).fetchall()
            for name, _ in triggers:
                conn.execute('DROP TRIGGER "' + name.replace('"', '""') + '"')
            conn.execute(f'UPDATE "{table}" SET rowid=?', (new_rowid,))
            for _, sql in triggers:
                conn.execute(sql)
        jobs = conn.execute("SELECT rowid,id,status,result_id FROM rule_validation_jobs ORDER BY rowid").fetchall()
        approvals = conn.execute("SELECT rowid,id,validation_id FROM rule_candidate_reviews ORDER BY rowid").fetchall()
    target = tmp_path / "migrated.sqlite"
    create_migrated_copy(account, review, target, maintenance_confirmed=True)
    with sqlite3.connect(target / "agent-t.sqlite") as conn:
        assert conn.execute("SELECT rowid,id,status,result_id FROM rule_validation_jobs ORDER BY rowid").fetchall() == jobs
        assert conn.execute("SELECT rowid,id,validation_id FROM rule_candidate_reviews ORDER BY rowid").fetchall() == approvals
        assert conn.execute("SELECT id FROM rule_candidate_reviews WHERE id=?", (decision["id"],)).fetchone()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM rule_candidates WHERE id=?", (candidate["id"],))
    incomplete = tmp_path / "missing-results-source.sqlite"
    with sqlite3.connect(account) as source, sqlite3.connect(incomplete) as copied:
        source.backup(copied)
    with sqlite3.connect(incomplete) as conn:
        conn.execute("DROP TABLE rule_validation_results")
    rejected_results = tmp_path / "missing-results-output.sqlite"
    # codex 二轮 P1-4 后：验证三表不完整在更早的完整性检查点被拒（消息更新，
    # 拒绝语义不变——「缺结果表」先命中三表齐全性闸，再到一致性核对）
    with pytest.raises(ValueError, match="不完整"):
        create_migrated_copy(incomplete, review, rejected_results, maintenance_confirmed=True)
    assert not rejected_results.exists()
    bad_generation = tmp_path / "bad-generation-source.sqlite"
    with sqlite3.connect(account) as source, sqlite3.connect(bad_generation) as copied:
        source.backup(copied)
    with sqlite3.connect(bad_generation) as conn:
        trigger = conn.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name='candidate_no_update'").fetchone()[0]
        conn.execute("DROP TRIGGER candidate_no_update")
        conn.execute("UPDATE rule_candidates SET base_generation=NULL WHERE id=?", (candidate["id"],))
        conn.execute(trigger)
    with pytest.raises(ValueError, match="测试与候选不一致"):
        create_migrated_copy(bad_generation, review, tmp_path / "bad-generation-output.sqlite", maintenance_confirmed=True)
    bad_decision = tmp_path / "bad-decision-source.sqlite"
    with sqlite3.connect(account) as source, sqlite3.connect(bad_decision) as copied:
        source.backup(copied)
    with sqlite3.connect(bad_decision) as conn:
        trigger = conn.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name='validation_no_update'").fetchone()[0]
        conn.execute("DROP TRIGGER validation_no_update")
        conn.execute("UPDATE rule_validation_results SET result_json=json_set(result_json,'$.passed',json('false')) WHERE id=?", (validation["id"],))
        conn.execute(trigger)
    with pytest.raises(ValueError, match="规则测试未通过"):
        create_migrated_copy(bad_decision, review, tmp_path / "bad-decision-output.sqlite", maintenance_confirmed=True)
    missing_review_validation = tmp_path / "missing-review-validation-source.sqlite"
    with sqlite3.connect(account) as source, sqlite3.connect(missing_review_validation) as copied:
        source.backup(copied)
    with sqlite3.connect(missing_review_validation) as conn:
        trigger = conn.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name='candidate_review_no_update'").fetchone()[0]
        conn.execute("DROP TRIGGER candidate_review_no_update")
        conn.execute("UPDATE rule_candidate_reviews SET validation_id=NULL WHERE id=?", (decision["id"],))
        conn.execute(trigger)
    with pytest.raises(ValueError, match="审核与候选不一致"):
        create_migrated_copy(missing_review_validation, review, tmp_path / "missing-review-validation-output.sqlite", maintenance_confirmed=True)
    bad_failure_list = tmp_path / "bad-failure-list-source.sqlite"
    with sqlite3.connect(account) as source, sqlite3.connect(bad_failure_list) as copied:
        source.backup(copied)
    with sqlite3.connect(bad_failure_list) as conn:
        trigger = conn.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name='validation_no_update'").fetchone()[0]
        conn.execute("DROP TRIGGER validation_no_update")
        conn.execute("UPDATE rule_validation_results SET result_json=json_set(result_json,'$.failed_test_ids','') WHERE id=?", (validation["id"],))
        conn.execute(trigger)
    with pytest.raises(ValueError, match="规则测试证据无效"):
        create_migrated_copy(bad_failure_list, review, tmp_path / "bad-failure-list-output.sqlite", maintenance_confirmed=True)
    with sqlite3.connect(account) as conn:
        trigger = conn.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name='candidate_no_update'").fetchone()[0]
        conn.execute("DROP TRIGGER candidate_no_update")
        conn.execute("UPDATE rule_candidates SET content=? WHERE id=?", (b"corrupted", candidate["id"]))
        conn.execute(trigger)
    rejected = tmp_path / "corrupted-copy.sqlite"
    with pytest.raises(ValueError, match="候选规则摘要无效"):
        create_migrated_copy(account, review, rejected, maintenance_confirmed=True)
    assert not rejected.exists()


def test_reference_subprocess_does_not_execute_sitecustomize(tmp_path, monkeypatch):
    import site
    from app.services import account_review_migration as migration

    user_site = site.getusersitepackages()
    system_site = migration.sysconfig.get_path("purelib")
    custom = tmp_path / "custom-site"
    custom.mkdir()
    marker = tmp_path / "site-ran"
    (custom / "sitecustomize.py").write_text(f"open({str(marker)!r}, 'w').write('ran')\n", encoding="utf-8")
    monkeypatch.setattr(migration.site, "getusersitepackages", lambda: str(custom))
    monkeypatch.setattr(migration.sysconfig, "get_path", lambda key: system_site if key == "purelib" else user_site)
    monkeypatch.setenv("STORE_DB_PATH", str(tmp_path / "reference-review.sqlite"))
    calls = []
    real_run = migration.subprocess.run

    def capture_command(command, **kwargs):
        calls.append(command)
        return real_run(command, **kwargs)

    monkeypatch.setattr(migration.subprocess, "run", capture_command)
    migration._reference(tmp_path / "reference-account.sqlite", tmp_path / "reference-review.sqlite", tmp_path)
    assert not marker.exists()
    assert "-I" in calls[0] and "-S" in calls[0] and "-B" in calls[0]


def test_migration_rejects_partial_validation_tables(source_databases, tmp_path):
    """codex 二轮 P1-4：验证三表（控制/任务/结果）部分缺失的源库拒绝迁移
    ——否则副本重建 blocked=0 控制表会把受阻安全闸恢复为可用。"""
    from app.services.rule_validation_service import RuleValidationService
    account_path, review_path, token, _, _, _ = source_databases
    RuleValidationService(AccountStore(account_path))  # 三表建全
    with sqlite3.connect(account_path) as conn:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("DROP TABLE rule_validation_control")  # 模拟损坏：只丢控制表
    with pytest.raises(ValueError, match="不完整"):
        create_migrated_copy(account_path, review_path, tmp_path / "new.sqlite",
                             maintenance_confirmed=True)


def test_migration_preserves_blocked_validation_gate(source_databases, tmp_path):
    """codex 二轮 P1-4：正常迁移必须原样保留 blocked=1 的风险闸状态与
    事件来源（控制/任务/结果行齐全的源库）。"""
    from app.services.rule_validation_service import RuleValidationService
    account_path, review_path, token, _, _, _ = source_databases
    RuleValidationService(AccountStore(account_path))
    with sqlite3.connect(account_path) as conn:
        conn.execute("UPDATE rule_validation_control SET blocked=1, reason='synthetic incident', "
                     "updated_at=1 WHERE id=1")
    target = tmp_path / "new.sqlite"
    create_migrated_copy(account_path, review_path, target, maintenance_confirmed=True)
    with sqlite3.connect(target / "agent-t.sqlite") as conn:
        row = conn.execute("SELECT blocked,reason FROM rule_validation_control WHERE id=1").fetchone()
        assert row == (1, "synthetic incident"), "受阻安全闸必须原样保留"
        # 防在线复位触发器随迁移重建（闸不能在副本里被改回）
        with pytest.raises(sqlite3.IntegrityError, match="offline recovery"):
            conn.execute("UPDATE rule_validation_control SET blocked=0 WHERE id=1")
            conn.commit()


def test_migration_rejects_missing_control_row(source_databases, tmp_path):
    """codex 三轮 P1-2：三表齐全但控制行（id=1）缺失 → 拒绝迁移。"""
    from app.services.rule_validation_service import RuleValidationService
    account_path, review_path, token, _, _, _ = source_databases
    RuleValidationService(AccountStore(account_path))
    with sqlite3.connect(account_path) as conn:
        # 结构全等模拟：摘触发器→删行→按原句重装触发器（_check_schema 的
        # 触发器存在性核对通过，只缺控制行——精确命中控制行完整性检查）
        trigger_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='validation_control_no_delete'").fetchone()[0]
        conn.execute("DROP TRIGGER validation_control_no_delete")
        conn.execute("DELETE FROM rule_validation_control WHERE id=1")
        conn.execute(trigger_sql)
    with pytest.raises(ValueError, match="控制行缺失"):
        create_migrated_copy(account_path, review_path, tmp_path / "new.sqlite",
                             maintenance_confirmed=True)
