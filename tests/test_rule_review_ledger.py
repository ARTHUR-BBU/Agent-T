"""Account-bound candidate review; no test result or publishing is implied."""
import hashlib
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest
import yaml

from app.services.account_store import AccountStore, AuthenticationError, AuthorizationError
from app.services.rule_review_ledger import RuleReviewLedger
from app.services.rule_snapshot import capture_rule_snapshot
from tests.helpers import create_active_account


@pytest.fixture
def setup_ledger(tmp_path):
    accounts = AccountStore(tmp_path / "accounts.sqlite")
    admin = accounts.bootstrap_admin("admin", "管理员", "test-admin-password")
    token = accounts.login("admin", "test-admin-password")
    user = create_active_account(accounts, token, "alice", "用户", "test-alice-password", "user")
    user_token = accounts.login("alice", "test-alice-password")
    return accounts, RuleReviewLedger(accounts), admin, token, user, user_token


def test_authenticated_submit_and_self_review_are_bound_to_exact_bytes(setup_ledger):
    _, ledger, admin, admin_token, _, _ = setup_ledger
    snapshot = capture_rule_snapshot("procurement")
    baseline = hashlib.sha256(snapshot.config_bytes).hexdigest()
    candidate = ledger.submit(admin_token, "procurement", snapshot.config_bytes, baseline)
    assert candidate["submitted_by"] == admin["id"]
    assert candidate["sha256"] == baseline
    review = ledger.review(admin_token, candidate["id"], candidate["sha256"], "approve", "通过人工审看")
    assert review["reviewer_id"] == admin["id"]
    assert review["self_review"] is True
    assert review["candidate_sha256"] == baseline
    saved = ledger.get(admin_token, candidate["id"])
    assert saved["review"]["id"] == review["id"]
    assert saved["content"] == snapshot.config_bytes == saved["baseline"]


def test_review_rejects_non_admin_stale_digest_and_second_decision(setup_ledger):
    accounts, ledger, _, admin_token, user, user_token = setup_ledger
    snapshot = capture_rule_snapshot("procurement")
    sha = hashlib.sha256(snapshot.config_bytes).hexdigest()
    candidate = ledger.submit(user_token, "procurement", snapshot.config_bytes, sha)
    assert candidate["submitted_by"] == user["id"]
    with pytest.raises(AuthorizationError):
        ledger.review(user_token, candidate["id"], sha, "approve", "不允许")
    with pytest.raises(ValueError):
        ledger.review(admin_token, candidate["id"], "0" * 64, "approve", "旧版本")
    ledger.review(admin_token, candidate["id"], sha, "reject", "需要修改")
    with pytest.raises(ValueError):
        ledger.review(admin_token, candidate["id"], sha, "approve", "重复")
    accounts.set_active(admin_token, user["id"], False)
    with pytest.raises(AuthenticationError):
        ledger.get(user_token, candidate["id"])


def test_candidate_privacy_and_validation(setup_ledger):
    accounts, ledger, _, admin_token, _, user_token = setup_ledger
    other = create_active_account(accounts, admin_token, "other", "其他用户", "test-other-password", "user")
    other_token = accounts.login("other", "test-other-password")
    snapshot = capture_rule_snapshot("procurement")
    sha = hashlib.sha256(snapshot.config_bytes).hexdigest()
    candidate = ledger.submit(user_token, "procurement", snapshot.config_bytes, sha)
    with pytest.raises(AuthorizationError):
        ledger.get(other_token, candidate["id"])
    assert ledger.get(admin_token, candidate["id"])["submitted_by"] != other["id"]
    with pytest.raises(ValueError):
        ledger.submit(user_token, "procurement", b"not yaml", sha)
    with pytest.raises(ValueError):
        ledger.submit(user_token, "procurement", snapshot.config_bytes, "wrong")


def test_concurrent_review_records_one_decision_and_tables_are_append_only(setup_ledger):
    accounts, ledger, _, admin_token, _, user_token = setup_ledger
    snapshot = capture_rule_snapshot("procurement")
    sha = hashlib.sha256(snapshot.config_bytes).hexdigest()
    candidate = ledger.submit(user_token, "procurement", snapshot.config_bytes, sha)

    def decide(_):
        try:
            return ledger.review(admin_token, candidate["id"], sha, "approve", "人工检查")
        except ValueError:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(decide, range(2)))
    assert sum(result is not None for result in results) == 1
    with accounts._connect() as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE rule_candidates SET content=? WHERE id=?", (b"changed", candidate["id"]))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM rule_candidate_reviews WHERE candidate_id=?", (candidate["id"],))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT OR REPLACE INTO rule_candidates SELECT * FROM rule_candidates WHERE id=?", (candidate["id"],))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT OR REPLACE INTO rule_candidate_reviews SELECT * FROM rule_candidate_reviews WHERE candidate_id=?", (candidate["id"],))


def test_review_rejects_changed_baseline(setup_ledger, monkeypatch):
    from app.services import rule_review_ledger

    _, ledger, _, admin_token, _, user_token = setup_ledger
    snapshot = capture_rule_snapshot("procurement")
    sha = hashlib.sha256(snapshot.config_bytes).hexdigest()
    candidate = ledger.submit(user_token, "procurement", snapshot.config_bytes, sha)
    monkeypatch.setattr(rule_review_ledger, "capture_rule_snapshot", lambda _: type("Snapshot", (), {"config_bytes": b"changed"})())
    with pytest.raises(ValueError, match="基线已变化"):
        ledger.review(admin_token, candidate["id"], sha, "approve", "人工检查")


def test_old_candidate_schema_is_rejected_without_silent_migration(tmp_path):
    accounts = AccountStore(tmp_path / "old.sqlite")
    with accounts._connect() as conn:
        conn.execute("CREATE TABLE rule_candidates (id TEXT PRIMARY KEY, category TEXT, content BLOB, sha256 TEXT, base_sha256 TEXT, submitted_by TEXT, created_at REAL)")
    with pytest.raises(RuntimeError, match="旧版"):
        RuleReviewLedger(accounts)
    with accounts._connect() as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(rule_candidates)")}
    assert "baseline" not in columns


def test_candidate_cannot_remove_hardline_or_inject_complex_regex(setup_ledger):
    _, ledger, _, _, _, token = setup_ledger
    snapshot = capture_rule_snapshot("procurement")
    baseline = hashlib.sha256(snapshot.config_bytes).hexdigest()
    config = snapshot.configuration()
    hardline = next(item for item in config["items"] if "hardline" in str(item))
    config["items"].remove(hardline)
    with pytest.raises(ValueError, match="硬性|删除"):
        ledger.submit(token, "procurement", yaml.safe_dump(config, allow_unicode=True).encode(), baseline)

    config = snapshot.configuration()
    config["items"].append({"id": "custom_new", "name": "新检查项", "description": "说明",
                            "class": "existence", "segment": "A",
                            "rules": {"pass": [{"pattern": "(a+)+$"}]}})
    with pytest.raises(ValueError, match="字面|正则"):
        ledger.submit(token, "procurement", yaml.safe_dump(config, allow_unicode=True).encode(), baseline)
    config["items"][-1]["rules"]["pass"][0]["pattern"] = re.escape("交付清单（附件）")
    accepted = ledger.submit(token, "procurement", yaml.safe_dump(config, allow_unicode=True).encode(), baseline)
    assert accepted["sha256"] != baseline


def test_direct_candidate_submission_cannot_bypass_duplicate_checks(setup_ledger):
    _, ledger, _, _, _, token = setup_ledger
    snapshot = capture_rule_snapshot("procurement")
    baseline = hashlib.sha256(snapshot.config_bytes).hexdigest()
    config = snapshot.configuration()
    config["items"].append({"id": "custom_duplicate", "name": " 主 体 ", "description": "新说明",
                            "class": "existence", "segment": "B", "rules": {"pass": [{"pattern": "新词"}]}})
    with pytest.raises(ValueError, match="名称.*重复"):
        ledger.submit(token, "procurement", yaml.safe_dump(config, allow_unicode=True).encode(), baseline)


def test_candidate_rejects_invalid_display_text_and_normalizes_hardline():
    from app.services.rule_review_ledger import _validate_candidate_changes

    baseline = {"items": [{"id": "x", "name": "原名称", "description": "原说明", "class": "existence"}]}
    changed = {"items": [{**baseline["items"][0], "name": {"unexpected": "mapping"}}]}
    with pytest.raises(ValueError, match="有效文本"):
        _validate_candidate_changes(baseline, changed)
    baseline["items"][0]["class"] = " HardLine "
    changed["items"][0]["class"] = " HardLine "
    changed["items"][0]["name"] = "新名称"
    with pytest.raises(ValueError, match="硬性"):
        _validate_candidate_changes(baseline, changed)


def test_candidate_can_disable_and_reenable_non_hardline_item(setup_ledger):
    _, ledger, _, _, _, token = setup_ledger
    snapshot = capture_rule_snapshot("procurement")
    digest = hashlib.sha256(snapshot.config_bytes).hexdigest()
    config = snapshot.configuration()
    item = next(i for i in config["items"] if i["id"] == "subject_matter")
    item["enabled"] = False
    submitted = ledger.submit(token, "procurement", yaml.safe_dump(config, allow_unicode=True).encode(), digest)
    assert submitted["sha256"] != digest
    item["enabled"] = True
    submitted = ledger.submit(token, "procurement", yaml.safe_dump(config, allow_unicode=True).encode(), digest)
    assert submitted["sha256"] != digest
