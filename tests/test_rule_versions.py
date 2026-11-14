"""Effective rule versions use an explicit database baseline, never mutable YAML pointers."""
import hashlib

import pytest

from app.services.account_store import AccountStore, AuthorizationError
from app.services.rule_snapshot import capture_rule_snapshot
from app.services.rule_versions import RuleVersionStore
from app.services.rule_review_ledger import RuleReviewLedger
from tests.helpers import create_active_account


def test_admin_imports_initial_version_once_and_reader_keeps_immutable_snapshot(tmp_path):
    accounts = AccountStore(tmp_path / "accounts.sqlite")
    accounts.bootstrap_admin("admin", "管理员", "testing-password-123")
    token = accounts.login("admin", "testing-password-123")
    user = create_active_account(accounts, token, "alice", "用户", "alice-password-123", "user")
    user_token = accounts.login("alice", "alice-password-123")
    versions = RuleVersionStore(accounts)
    raw = capture_rule_snapshot("procurement").config_bytes
    with pytest.raises(AuthorizationError):
        versions.initialize(user_token, "procurement", raw)
    version = versions.initialize(token, "procurement", raw)
    assert version["sha256"] == hashlib.sha256(raw).hexdigest()
    assert version["actor_id"] != user["id"]
    snapshot = versions.current("procurement")
    assert snapshot.config_bytes == raw
    with pytest.raises(ValueError, match="已初始化"):
        versions.initialize(token, "procurement", raw)
    assert versions.current("procurement").config_bytes == raw


def test_missing_and_corrupt_active_versions_fail_closed(tmp_path):
    accounts = AccountStore(tmp_path / "accounts.sqlite")
    accounts.bootstrap_admin("admin", "管理员", "testing-password-123")
    token = accounts.login("admin", "testing-password-123")
    versions = RuleVersionStore(accounts)
    with pytest.raises(ValueError, match="未初始化"):
        versions.current("procurement")
    raw = capture_rule_snapshot("procurement").config_bytes
    versions.initialize(token, "procurement", raw)
    with accounts._connect() as conn:
        with pytest.raises(Exception, match="append-only"):
            conn.execute("UPDATE rule_versions SET content=?", (b"bad",))
        with pytest.raises(Exception, match="append-only"):
            conn.execute("INSERT OR REPLACE INTO rule_versions SELECT * FROM rule_versions")
        with pytest.raises(Exception, match="generation must advance"):
            conn.execute("UPDATE rule_active_versions SET generation=generation")
        with pytest.raises(Exception, match="cannot be replaced"):
            conn.execute("INSERT OR REPLACE INTO rule_active_versions SELECT * FROM rule_active_versions")


def test_versioned_ledger_uses_database_baseline_not_later_disk_changes(tmp_path, monkeypatch):
    from app.services import checklist

    source = checklist.CONFIG_DIR / "checklist_procurement.yaml"
    path = tmp_path / source.name
    path.write_bytes(source.read_bytes())
    monkeypatch.setattr(checklist, "CONFIG_DIR", tmp_path)
    accounts = AccountStore(tmp_path / "accounts.sqlite")
    accounts.bootstrap_admin("admin", "管理员", "testing-password-123")
    token = accounts.login("admin", "testing-password-123")
    versions = RuleVersionStore(accounts)
    raw = path.read_bytes()
    version = versions.initialize(token, "procurement", raw)
    ledger = RuleReviewLedger(accounts, versioned=True)
    path.write_bytes(raw + b"\n# file changed after import\n")
    candidate = ledger.submit(token, "procurement", raw, version["sha256"])
    review = ledger.review(token, candidate["id"], candidate["sha256"], "reject", "人工检查")
    assert review["self_review"] is True


def test_versioned_review_rejects_baseline_pointer_change(tmp_path):
    import time
    import uuid

    accounts = AccountStore(tmp_path / "accounts.sqlite")
    admin = accounts.bootstrap_admin("admin", "管理员", "testing-password-123")
    token = accounts.login("admin", "testing-password-123")
    raw = capture_rule_snapshot("procurement").config_bytes
    versions = RuleVersionStore(accounts)
    initial = versions.initialize(token, "procurement", raw)
    ledger = RuleReviewLedger(accounts, versioned=True)
    candidate = ledger.submit(token, "procurement", raw, initial["sha256"])
    new_raw = raw + b"\n# a new version\n"
    new_id = uuid.uuid4().hex
    with accounts._connect() as conn:
        conn.execute("INSERT INTO rule_versions VALUES (?,?,?,?,?,?)", (new_id, "procurement", hashlib.sha256(new_raw).hexdigest(), new_raw, admin["id"], time.time()))
        conn.execute("UPDATE rule_active_versions SET version_id=?, generation=generation+1 WHERE category=?", (new_id, "procurement"))
    with pytest.raises(ValueError, match="基线已变化"):
        ledger.review(token, candidate["id"], candidate["sha256"], "approve", "人工检查")
    with pytest.raises(ValueError, match="基线已变化"):
        ledger.submit(token, "procurement", raw, initial["sha256"])
    with accounts._connect() as conn:
        conn.execute("UPDATE rule_active_versions SET version_id=?, generation=generation+1 WHERE category=?", (initial["id"], "procurement"))
    with pytest.raises(ValueError, match="基线已变化"):
        ledger.review(token, candidate["id"], candidate["sha256"], "approve", "旧审核不能复活")


def test_captured_version_metadata_stays_bound_to_bytes_across_switch_and_rollback(tmp_path):
    import time
    import uuid

    accounts = AccountStore(tmp_path / "accounts.sqlite")
    admin = accounts.bootstrap_admin("admin", "管理员", "testing-password-123")
    token = accounts.login("admin", "testing-password-123")
    versions = RuleVersionStore(accounts)
    raw = capture_rule_snapshot("procurement").config_bytes
    initial = versions.initialize(token, "procurement", raw)
    captured = versions.capture_current("procurement")
    new_raw = raw + b"\n# changed version\n"
    new_id = uuid.uuid4().hex
    with accounts._connect() as conn:
        conn.execute("INSERT INTO rule_versions VALUES (?,?,?,?,?,?)", (new_id, "procurement", hashlib.sha256(new_raw).hexdigest(), new_raw, admin["id"], time.time()))
        conn.execute("UPDATE rule_active_versions SET version_id=?, generation=generation+1 WHERE category=?", (new_id, "procurement"))
    changed = versions.capture_current("procurement")
    with accounts._connect() as conn:
        conn.execute("UPDATE rule_active_versions SET version_id=?, generation=generation+1 WHERE category=?", (initial["id"], "procurement"))
    restored = versions.capture_current("procurement")
    assert (captured.version_id, captured.generation, captured.sha256, captured.snapshot.config_bytes) == (initial["id"], 1, hashlib.sha256(raw).hexdigest(), raw)
    assert (changed.version_id, changed.generation, changed.sha256, changed.snapshot.config_bytes) == (new_id, 2, hashlib.sha256(new_raw).hexdigest(), new_raw)
    assert (restored.version_id, restored.generation, restored.sha256) == (initial["id"], 3, captured.sha256)


def test_concurrent_capture_never_mixes_pointer_metadata_and_content(tmp_path):
    import threading
    import time
    import uuid
    from concurrent.futures import ThreadPoolExecutor

    accounts = AccountStore(tmp_path / "accounts.sqlite")
    admin = accounts.bootstrap_admin("admin", "管理员", "testing-password-123")
    token = accounts.login("admin", "testing-password-123")
    versions = RuleVersionStore(accounts)
    first_raw = capture_rule_snapshot("procurement").config_bytes
    first = versions.initialize(token, "procurement", first_raw)
    second_raw = first_raw + b"\n# another version\n"
    second_id = uuid.uuid4().hex
    with accounts._connect() as conn:
        conn.execute("INSERT INTO rule_versions VALUES (?,?,?,?,?,?)", (second_id, "procurement", hashlib.sha256(second_raw).hexdigest(), second_raw, admin["id"], time.time()))
    start = threading.Barrier(3)

    def switch():
        start.wait()
        for generation in range(2, 22):
            version_id = second_id if generation % 2 == 0 else first["id"]
            with accounts._connect() as conn:
                conn.execute("UPDATE rule_active_versions SET version_id=?, generation=generation+1 WHERE category=?", (version_id, "procurement"))

    def capture_many():
        start.wait()
        return [versions.capture_current("procurement") for _ in range(20)]

    with ThreadPoolExecutor(max_workers=3) as pool:
        writer = pool.submit(switch)
        readers = [pool.submit(capture_many) for _ in range(2)]
        writer.result()
        captured = [entry for reader in readers for entry in reader.result()]
    for entry in captured:
        expected_id, expected_bytes = (second_id, second_raw) if entry.generation % 2 == 0 else (first["id"], first_raw)
        assert entry.version_id == expected_id
        assert entry.snapshot.config_bytes == expected_bytes
        assert entry.sha256 == hashlib.sha256(expected_bytes).hexdigest()


def test_unversioned_candidate_cannot_be_reviewed_as_versioned(tmp_path):
    accounts = AccountStore(tmp_path / "accounts.sqlite")
    accounts.bootstrap_admin("admin", "管理员", "testing-password-123")
    token = accounts.login("admin", "testing-password-123")
    raw = capture_rule_snapshot("procurement").config_bytes
    digest = hashlib.sha256(raw).hexdigest()
    candidate = RuleReviewLedger(accounts).submit(token, "procurement", raw, digest)
    assert candidate["base_generation"] is None
    RuleVersionStore(accounts).initialize(token, "procurement", raw)
    with pytest.raises(ValueError, match="基线已变化"):
        RuleReviewLedger(accounts, versioned=True).review(token, candidate["id"], digest, "approve", "不能升级旧审批")
