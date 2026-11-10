"""Versioned draft submission is authenticated and atomically records its rationale."""
import hashlib
import sqlite3
import uuid

import pytest

from app.services.account_store import AccountStore, AuthorizationError
from app.services.rule_snapshot import capture_rule_snapshot
from app.services.rule_submission import RuleSubmissionService
from app.services.rule_versions import RuleVersionStore
from tests.helpers import create_active_account


@pytest.fixture
def setup(tmp_path):
    accounts = AccountStore(tmp_path / "accounts.sqlite")
    admin = accounts.bootstrap_admin("admin", "管理员", "testing-password-123")
    admin_token = accounts.login("admin", "testing-password-123")
    alice = create_active_account(accounts, admin_token, "alice", "用户甲", "alice-password-123", "user")
    create_active_account(accounts, admin_token, "bob", "用户乙", "bobby-password-123", "user")
    alice_token = accounts.login("alice", "alice-password-123")
    bob_token = accounts.login("bob", "bobby-password-123")
    snapshot = capture_rule_snapshot("procurement")
    RuleVersionStore(accounts).initialize(admin_token, "procurement", snapshot.config_bytes)
    return accounts, RuleSubmissionService(accounts), admin, admin_token, alice, alice_token, bob_token, snapshot


def test_submit_persists_server_generated_candidate_examples_and_actor(setup):
    _, service, _, admin, alice, token, bob, snapshot = setup
    draft = {"category": "procurement", "operation": "add", "item_id": "custom_delivery",
             "name": "交付清单", "description": "检查交付清单", "reason": "明确交付范围",
             "segment": "B", "literal": "交付清单", "positive": "依照交付清单验收",
             "negative": "仅安排验收", "base_sha256": hashlib.sha256(snapshot.config_bytes).hexdigest(),
             "submitted_by": "forged", "validation": {"passed": True}}
    candidate = service.submit(token, draft, expected_generation=1, request_id=uuid.uuid4().hex)
    assert candidate["submitted_by"] == alice["id"] and candidate["base_generation"] == 1
    details = service.get(token, candidate["id"])
    assert details["reason"] == "明确交付范围"
    assert details["examples"]["results"] == {"positive": "通过", "negative": "未找到"}
    assert details["change"]["after"]["literal"] == "交付清单"
    assert service.get(admin, candidate["id"])["submitted_by"] == alice["id"]
    with pytest.raises(AuthorizationError):
        service.get(bob, candidate["id"])


def test_rejects_old_generation_and_failed_examples_without_partial_record(setup):
    accounts, service, _, _, _, token, _, snapshot = setup
    draft = {"category": "procurement", "operation": "add", "item_id": "custom_delivery",
             "name": "交付清单", "description": "检查交付清单", "reason": "明确交付范围",
             "segment": "B", "literal": "交付清单", "positive": "依照交付清单验收",
             "negative": "也有交付清单", "base_sha256": hashlib.sha256(snapshot.config_bytes).hexdigest()}
    with pytest.raises(ValueError, match="代次"):
        service.submit(token, draft, expected_generation=2, request_id=uuid.uuid4().hex)
    with pytest.raises(ValueError, match="正反例"):
        service.submit(token, draft, expected_generation=1, request_id=uuid.uuid4().hex)
    with accounts._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM rule_candidates").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM rule_candidate_details").fetchone()[0] == 0


def test_session_revoked_during_build_cannot_submit(setup, monkeypatch):
    from app.services import rule_submission
    from app.services.account_store import AuthenticationError

    accounts, service, _, admin, alice, token, _, snapshot = setup
    draft = {"category": "procurement", "operation": "add", "item_id": "custom_delivery",
             "name": "交付清单", "description": "检查交付清单", "reason": "明确交付范围",
             "segment": "B", "literal": "交付清单", "positive": "依照交付清单验收",
             "negative": "仅安排验收", "base_sha256": hashlib.sha256(snapshot.config_bytes).hexdigest()}
    original = rule_submission.build_candidate

    def build_and_revoke(base, fields):
        result = original(base, fields)
        accounts.set_active(admin, alice["id"], False)
        return result

    monkeypatch.setattr(rule_submission, "build_candidate", build_and_revoke)
    with pytest.raises(AuthenticationError):
        service.submit(token, draft, expected_generation=1, request_id=uuid.uuid4().hex)
    with accounts._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM rule_candidates").fetchone()[0] == 0


def test_details_write_failure_rolls_back_candidate(setup):
    accounts, service, _, _, _, token, _, snapshot = setup
    draft = {"category": "procurement", "operation": "add", "item_id": "custom_delivery",
             "name": "交付清单", "description": "检查交付清单", "reason": "明确交付范围",
             "segment": "B", "literal": "交付清单", "positive": "依照交付清单验收",
             "negative": "仅安排验收", "base_sha256": hashlib.sha256(snapshot.config_bytes).hexdigest()}
    with accounts._connect() as conn:
        conn.execute("CREATE TRIGGER reject_details BEFORE INSERT ON rule_candidate_details BEGIN SELECT RAISE(ABORT, 'forced failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match="forced failure"):
        service.submit(token, draft, expected_generation=1, request_id=uuid.uuid4().hex)
    with accounts._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM rule_candidates").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM rule_candidate_details").fetchone()[0] == 0


def test_retry_with_same_request_id_is_idempotent_and_conflicting_reuse_fails(setup):
    accounts, service, _, _, _, token, _, snapshot = setup
    draft = {"category": "procurement", "operation": "add", "item_id": "custom_delivery",
             "name": "交付清单", "description": "检查交付清单", "reason": "明确交付范围",
             "segment": "B", "literal": "交付清单", "positive": "依照交付清单验收",
             "negative": "仅安排验收", "base_sha256": hashlib.sha256(snapshot.config_bytes).hexdigest()}
    request_id = uuid.uuid4().hex
    first = service.submit(token, draft, expected_generation=1, request_id=request_id)
    second = service.submit(token, draft, expected_generation=1, request_id=request_id)
    assert first == second
    with pytest.raises(ValueError, match="请求编号"):
        service.submit(token, {**draft, "reason": "不同理由"}, expected_generation=1, request_id=request_id)
    with accounts._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM rule_candidates").fetchone()[0] == 1


def test_concurrent_retry_creates_one_candidate(setup):
    from concurrent.futures import ThreadPoolExecutor

    accounts, service, _, _, _, token, _, snapshot = setup
    draft = {"category": "procurement", "operation": "add", "item_id": "custom_delivery",
             "name": "交付清单", "description": "检查交付清单", "reason": "明确交付范围",
             "segment": "B", "literal": "交付清单", "positive": "依照交付清单验收",
             "negative": "仅安排验收", "base_sha256": hashlib.sha256(snapshot.config_bytes).hexdigest()}
    request_id = uuid.uuid4().hex
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: service.submit(token, draft, expected_generation=1, request_id=request_id), range(2)))
    assert results[0] == results[1]
    with accounts._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM rule_candidates").fetchone()[0] == 1


def test_retry_rejects_corrupted_stored_candidate(setup):
    accounts, service, _, _, _, token, _, snapshot = setup
    draft = {"category": "procurement", "operation": "add", "item_id": "custom_delivery",
             "name": "交付清单", "description": "检查交付清单", "reason": "明确交付范围",
             "segment": "B", "literal": "交付清单", "positive": "依照交付清单验收",
             "negative": "仅安排验收", "base_sha256": hashlib.sha256(snapshot.config_bytes).hexdigest()}
    request_id = uuid.uuid4().hex
    service.submit(token, draft, expected_generation=1, request_id=request_id)
    with accounts._connect() as conn:
        conn.execute("DROP TRIGGER candidate_no_update")
        conn.execute("UPDATE rule_candidates SET content=?", (b"corrupt",))
    with pytest.raises(ValueError, match="损坏"):
        service.submit(token, draft, expected_generation=1, request_id=request_id)
