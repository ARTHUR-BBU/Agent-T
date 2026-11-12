"""Gold checks are run by the server and bound to the exact versioned candidate."""
import hashlib
import os
import sqlite3
import subprocess
import sys
import uuid

import pytest

from app.services.account_store import AccountStore, AuthenticationError, AuthorizationError
from app.services.rule_candidate_validation import GoldCheckResult
from app.services.rule_snapshot import capture_rule_snapshot
from app.services.rule_submission import RuleSubmissionService
from app.services.rule_validation_service import RuleValidationService
from app.services.rule_versions import RuleVersionStore
from app.services.rule_review_ledger import RuleReviewLedger
from tests.helpers import create_active_account


@pytest.fixture
def setup(tmp_path, monkeypatch):
    from app.services import rule_review_ledger

    monkeypatch.setattr(rule_review_ledger, "current_input_manifest", lambda snapshot: "a" * 64)
    accounts = AccountStore(tmp_path / "accounts.sqlite")
    accounts.bootstrap_admin("admin", "管理员", "testing-password-123")
    admin = accounts.login("admin", "testing-password-123")
    alice = create_active_account(accounts, admin, "alice", "甲", "alice-password-123", "user")
    create_active_account(accounts, admin, "bob", "乙", "bobby-password-123", "user")
    token = accounts.login("alice", "alice-password-123")
    bob = accounts.login("bob", "bobby-password-123")
    snapshot = capture_rule_snapshot("procurement")
    RuleVersionStore(accounts).initialize(admin, "procurement", snapshot.config_bytes)
    draft = {"category": "procurement", "operation": "add", "item_id": "custom_delivery",
             "name": "交付清单", "description": "检查交付清单", "reason": "明确交付范围",
             "segment": "B", "literal": "交付清单", "positive": "依照交付清单验收",
             "negative": "仅安排验收", "base_sha256": hashlib.sha256(snapshot.config_bytes).hexdigest()}
    candidate = RuleSubmissionService(accounts).submit(token, draft, expected_generation=1, request_id=uuid.uuid4().hex)
    return accounts, RuleValidationService(accounts), admin, alice, token, bob, candidate


def test_result_is_computed_server_side_and_visible_only_to_author_or_admin(setup, monkeypatch):
    from app.services import rule_validation_service

    accounts, service, admin, alice, token, bob, candidate = setup
    called = []

    def runner(snapshot):
        digest = hashlib.sha256(snapshot.config_bytes).hexdigest()
        called.append(digest)
        return GoldCheckResult(category=snapshot.category, candidate_sha256=digest,
                               input_manifest_sha256="a" * 64, suite="tests/gold.py", passed=True,
                               passed_tests=3, collected_tests=3, skipped_tests=0, failed_test_ids=(),
                               exit_code=0, output_sha256="b" * 64)

    monkeypatch.setattr(rule_validation_service, "run_gold_checks", runner)
    ledger = RuleReviewLedger(accounts, versioned=True)
    with pytest.raises(ValueError, match="测试"):
        ledger.review(admin, candidate["id"], candidate["sha256"], "approve", "人工检查")
    with pytest.raises(AuthorizationError):
        service.validate(bob, candidate["id"])
    result = service.validate(token, candidate["id"])
    assert called == [candidate["sha256"]]
    assert result["actor_id"] == alice["id"] and result["candidate_sha256"] == candidate["sha256"]
    assert result["base_generation"] == 1 and result["passed"] is True
    review = ledger.review(admin, candidate["id"], candidate["sha256"], "approve", "人工检查", expected_validation_id=result["id"])
    assert review["decision"] == "approve" and review["validation_id"] == result["id"]
    assert service.list_results(admin, candidate["id"])[0]["id"] == result["id"]
    with pytest.raises(AuthorizationError):
        service.list_results(bob, candidate["id"])


def test_revocation_during_gold_run_prevents_recording(setup, monkeypatch):
    from app.services import rule_validation_service

    accounts, service, admin, alice, token, _, candidate = setup

    def runner(snapshot):
        accounts.set_active(admin, alice["id"], False)
        return GoldCheckResult(category=snapshot.category, candidate_sha256=hashlib.sha256(snapshot.config_bytes).hexdigest(),
                               input_manifest_sha256="a" * 64, suite="tests/gold.py", passed=True,
                               passed_tests=3, collected_tests=3, skipped_tests=0, failed_test_ids=(),
                               exit_code=0, output_sha256="b" * 64)

    monkeypatch.setattr(rule_validation_service, "run_gold_checks", runner)
    with pytest.raises(AuthenticationError):
        service.validate(token, candidate["id"])
    with accounts._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM rule_validation_results").fetchone()[0] == 0


def test_new_test_result_requires_new_review_and_failed_result_cannot_be_approved(setup, monkeypatch):
    from app.services import rule_validation_service

    accounts, service, admin, _, token, _, candidate = setup
    state = {"passed": True}

    def runner(snapshot):
        passed = state["passed"]
        return GoldCheckResult(category=snapshot.category, candidate_sha256=hashlib.sha256(snapshot.config_bytes).hexdigest(),
                               input_manifest_sha256="a" * 64, suite="tests/gold.py", passed=passed,
                               passed_tests=3 if passed else 2, collected_tests=3, skipped_tests=0,
                               failed_test_ids=() if passed else ("test_failed",), exit_code=0 if passed else 1,
                               output_sha256="b" * 64)

    monkeypatch.setattr(rule_validation_service, "run_gold_checks", runner)
    ledger = RuleReviewLedger(accounts, versioned=True)
    first = service.validate(token, candidate["id"])
    first_review = ledger.review(admin, candidate["id"], candidate["sha256"], "approve", "审核首次测试", expected_validation_id=first["id"])
    assert first_review["validation_id"] == first["id"]
    state["passed"] = False
    service.validate(token, candidate["id"])
    with pytest.raises(ValueError, match="测试"):
        ledger.review(admin, candidate["id"], candidate["sha256"], "approve", "不能忽略失败")
    state["passed"] = True
    latest = service.validate(token, candidate["id"])
    with pytest.raises(ValueError, match="测试结果已变化"):
        ledger.review(admin, candidate["id"], candidate["sha256"], "approve", "错误使用旧结果", expected_validation_id=first["id"])
    second_review = ledger.review(admin, candidate["id"], candidate["sha256"], "approve", "复核最新测试", expected_validation_id=latest["id"])
    assert second_review["validation_id"] == latest["id"] and second_review["id"] != first_review["id"]
    assert ledger.get(admin, candidate["id"])["review"]["id"] == second_review["id"]


def test_validation_slot_is_held_until_result_is_committed(setup, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    from app.services import rule_validation_service

    accounts, service, _, _, token, _, candidate = setup
    entered, release = threading.Event(), threading.Event()

    def runner(snapshot):
        return GoldCheckResult(category=snapshot.category, candidate_sha256=hashlib.sha256(snapshot.config_bytes).hexdigest(),
                               input_manifest_sha256="a" * 64, suite="tests/gold.py", passed=True,
                               passed_tests=3, collected_tests=3, skipped_tests=0, failed_test_ids=(),
                               exit_code=0, output_sha256="b" * 64)

    original_audit = accounts._audit

    def block_audit(conn, actor_id, action, target_id):
        if action == "validate_rule_candidate":
            entered.set()
            assert release.wait(timeout=20)
        original_audit(conn, actor_id, action, target_id)

    monkeypatch.setattr(rule_validation_service, "run_gold_checks", runner)
    monkeypatch.setattr(accounts, "_audit", block_audit)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(service.validate, token, candidate["id"])
        try:
            assert entered.wait(timeout=10)
            with pytest.raises(RuntimeError, match="正在测试"):
                service.validate(token, candidate["id"])
        finally:
            release.set()
        assert future.result(timeout=10)["passed"] is True


def test_pending_validation_blocks_approval_of_previous_result(setup, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    from app.services import rule_validation_service

    accounts, service, admin, _, token, _, candidate = setup
    entered, release = threading.Event(), threading.Event()
    block = {"enabled": False}

    def runner(snapshot):
        if block["enabled"]:
            entered.set()
            assert release.wait(timeout=20)
        return GoldCheckResult(category=snapshot.category, candidate_sha256=hashlib.sha256(snapshot.config_bytes).hexdigest(),
                               input_manifest_sha256="a" * 64, suite="tests/gold.py", passed=True,
                               passed_tests=3, collected_tests=3, skipped_tests=0, failed_test_ids=(),
                               exit_code=0, output_sha256="b" * 64)

    monkeypatch.setattr(rule_validation_service, "run_gold_checks", runner)
    first = service.validate(token, candidate["id"])
    ledger = RuleReviewLedger(accounts, versioned=True)
    assert ledger.get(admin, candidate["id"])["status"] == "待审核"
    ledger.review(admin, candidate["id"], candidate["sha256"], "approve", "首次审核", expected_validation_id=first["id"])
    assert ledger.get(admin, candidate["id"])["status"] == "已审核"
    block["enabled"] = True
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(service.validate, token, candidate["id"])
        try:
            assert entered.wait(timeout=10)
            assert ledger.get(admin, candidate["id"])["status"] == "测试中"
            with pytest.raises(RuntimeError, match="测试"):
                ledger.review(admin, candidate["id"], candidate["sha256"], "approve", "不能使用旧结果")
        finally:
            release.set()
        assert future.result(timeout=10)["passed"] is True


def test_database_lease_blocks_other_workers_and_requires_expired_job_inspection(setup, monkeypatch):
    import time
    from app.services import rule_validation_service

    accounts, service, _, alice, token, _, candidate = setup
    calls = []

    def runner(snapshot):
        calls.append(True)
        return GoldCheckResult(category=snapshot.category, candidate_sha256=hashlib.sha256(snapshot.config_bytes).hexdigest(),
                               input_manifest_sha256="a" * 64, suite="tests/gold.py", passed=True,
                               passed_tests=3, collected_tests=3, skipped_tests=0, failed_test_ids=(),
                               exit_code=0, output_sha256="b" * 64)

    monkeypatch.setattr(rule_validation_service, "run_gold_checks", runner)
    now = time.time()
    with accounts._connect() as conn:
        conn.execute("INSERT INTO rule_validation_jobs VALUES (?,?,?,?,?,?,?)",
                     ("running-job", candidate["id"], alice["id"], "running", now, now + 60, None))
    with pytest.raises(RuntimeError, match="正在测试"):
        service.validate(token, candidate["id"])
    assert calls == []
    monkeypatch.setattr(rule_validation_service.time, "time", lambda: now + 61)
    with pytest.raises(RuntimeError, match="检查进程"):
        service.validate(token, candidate["id"])
    with accounts._connect() as conn:
        states = [row[0] for row in conn.execute("SELECT status FROM rule_validation_jobs ORDER BY rowid")]
        blocked = conn.execute("SELECT blocked FROM rule_validation_control WHERE id=1").fetchone()[0]
    assert states == ["abandoned"] and blocked == 1
    with pytest.raises(RuntimeError, match="待检查"):
        RuleValidationService(accounts).validate(token, candidate["id"])
    assert calls == []


def test_expired_job_is_shown_as_retry_needed_and_rejected_candidate_cannot_run(setup):
    import time

    accounts, service, admin, alice, token, _, candidate = setup
    now = time.time()
    with accounts._connect() as conn:
        conn.execute("INSERT INTO rule_validation_jobs VALUES (?,?,?,?,?,?,?)",
                     ("expired-job", candidate["id"], alice["id"], "running", now - 20, now - 1, None))
    ledger = RuleReviewLedger(accounts, versioned=True)
    assert ledger.get(admin, candidate["id"])["status"] == "测试已中断，请检查后恢复"
    ledger.review(admin, candidate["id"], candidate["sha256"], "reject", "请重新提交")
    with pytest.raises(ValueError, match="已退回"):
        service.validate(token, candidate["id"])


def _unfinished(snapshot):
    return GoldCheckResult(category=snapshot.category, candidate_sha256=hashlib.sha256(snapshot.config_bytes).hexdigest(),
                           input_manifest_sha256="a" * 64, suite="tests/gold.py", passed=False,
                           passed_tests=0, collected_tests=0, skipped_tests=0, failed_test_ids=(),
                           exit_code=None, output_sha256=None)


def _passing(snapshot):
    return GoldCheckResult(category=snapshot.category, candidate_sha256=hashlib.sha256(snapshot.config_bytes).hexdigest(),
                           input_manifest_sha256="a" * 64, suite="tests/gold.py", passed=True,
                           passed_tests=3, collected_tests=3, skipped_tests=0, failed_test_ids=(),
                           exit_code=0, output_sha256="b" * 64)


def test_unfinished_test_blocks_new_worker_and_survives_restart(setup, monkeypatch, tmp_path):
    from app.services import rule_validation_service

    accounts, service, admin, _, token, _, candidate = setup
    monkeypatch.setattr(rule_validation_service, "run_gold_checks", _unfinished)
    result = service.validate(token, candidate["id"])
    assert result["passed"] is False
    assert RuleReviewLedger(accounts, versioned=True).get(admin, candidate["id"])["status"] == "测试已中断，请检查后恢复"
    with accounts._connect() as conn:
        blocked = conn.execute("SELECT blocked,reason,last_job_id FROM rule_validation_control WHERE id=1").fetchone()
        assert blocked["blocked"] == 1 and blocked["reason"] == "uncertain_process" and blocked["last_job_id"]
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE rule_validation_control SET blocked=0 WHERE id=1")
    with pytest.raises(RuntimeError, match="待检查"):
        RuleValidationService(accounts).validate(token, candidate["id"])
    env = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP", "APPDATA", "USERPROFILE") if key in os.environ}
    env.update(PYTHONUTF8="1", STORE_DB_PATH=str(tmp_path / "separate.sqlite"), TEST_DB=str(accounts.path),
               TEST_TOKEN=token, TEST_CANDIDATE=candidate["id"])
    script = """
import os
from pathlib import Path
from app.services.account_store import AccountStore
from app.services.rule_validation_service import RuleValidationService
try:
    RuleValidationService(AccountStore(Path(os.environ['TEST_DB']))).validate(os.environ['TEST_TOKEN'], os.environ['TEST_CANDIDATE'])
except RuntimeError as exc:
    assert '待检查' in str(exc)
else:
    raise AssertionError('persisted block not enforced')
"""
    proc = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr


def test_failed_block_write_leaves_job_running(setup, monkeypatch):
    from app.services import rule_validation_service

    accounts, service, _, _, token, _, candidate = setup
    monkeypatch.setattr(rule_validation_service, "run_gold_checks", _unfinished)
    with accounts._connect() as conn:
        conn.execute("CREATE TRIGGER test_block_write_failure BEFORE UPDATE ON rule_validation_control "
                     "WHEN NEW.blocked=1 BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        service.validate(token, candidate["id"])
    with accounts._connect() as conn:
        assert conn.execute("SELECT status FROM rule_validation_jobs").fetchone()[0] == "running"


@pytest.mark.parametrize("condition", ["blocked", "running"])
def test_incident_blocks_approval_but_allows_rejection(setup, monkeypatch, condition):
    from app.services import rule_validation_service

    accounts, service, admin, _, token, _, candidate = setup

    def passed(snapshot):
        return GoldCheckResult(category=snapshot.category, candidate_sha256=hashlib.sha256(snapshot.config_bytes).hexdigest(),
                               input_manifest_sha256="a" * 64, suite="tests/gold.py", passed=True,
                               passed_tests=1, collected_tests=1, skipped_tests=0, failed_test_ids=(),
                               exit_code=0, output_sha256="b" * 64)

    monkeypatch.setattr(rule_validation_service, "run_gold_checks", passed)
    result = service.validate(token, candidate["id"])
    with accounts._connect() as conn:
        if condition == "blocked":
            conn.execute("UPDATE rule_validation_control SET blocked=1,reason='uncertain_process',updated_at=1 WHERE id=1")
        else:
            conn.execute("INSERT INTO rule_validation_jobs VALUES (?,?,?,?,?,?,?)",
                         ("other-running", candidate["id"], candidate["submitted_by"], "running", 1, 9999999999, None))
    ledger = RuleReviewLedger(accounts, versioned=True)
    with pytest.raises(RuntimeError, match="待检查"):
        ledger.review(admin, candidate["id"], candidate["sha256"], "approve", "通过", expected_validation_id=result["id"])
    assert ledger.review(admin, candidate["id"], candidate["sha256"], "reject", "需要修改")["decision"] == "reject"


def test_changed_test_inputs_invalidate_review_status_and_approval(setup, monkeypatch):
    from app.services import rule_review_ledger, rule_validation_service

    accounts, service, admin, _, token, _, candidate = setup

    def runner(snapshot):
        return GoldCheckResult(category=snapshot.category, candidate_sha256=hashlib.sha256(snapshot.config_bytes).hexdigest(),
                               input_manifest_sha256="a" * 64, suite="tests/gold.py", passed=True,
                               passed_tests=3, collected_tests=3, skipped_tests=0, failed_test_ids=(),
                               exit_code=0, output_sha256="b" * 64)

    monkeypatch.setattr(rule_validation_service, "run_gold_checks", runner)
    result = service.validate(token, candidate["id"])
    ledger = RuleReviewLedger(accounts, versioned=True)
    assert ledger.get(admin, candidate["id"])["status"] == "待审核"
    monkeypatch.setattr(rule_review_ledger, "current_input_manifest", lambda snapshot: "c" * 64)
    assert ledger.get(admin, candidate["id"])["status"] == "测试环境已变化"
    with pytest.raises(ValueError, match="测试环境已变化"):
        ledger.review(admin, candidate["id"], candidate["sha256"], "approve", "人工审核", expected_validation_id=result["id"])


def test_environment_check_does_not_lock_account_writes_and_rechecks_session(setup, monkeypatch):
    from app.services import rule_review_ledger, rule_validation_service

    accounts, service, admin, alice, token, _, candidate = setup

    def runner(snapshot):
        return GoldCheckResult(category=snapshot.category, candidate_sha256=hashlib.sha256(snapshot.config_bytes).hexdigest(),
                               input_manifest_sha256="a" * 64, suite="tests/gold.py", passed=True,
                               passed_tests=3, collected_tests=3, skipped_tests=0, failed_test_ids=(),
                               exit_code=0, output_sha256="b" * 64)

    monkeypatch.setattr(rule_validation_service, "run_gold_checks", runner)
    service.validate(token, candidate["id"])

    def revoke_during_manifest(snapshot):
        accounts.set_active(admin, alice["id"], False)
        return "a" * 64

    monkeypatch.setattr(rule_review_ledger, "current_input_manifest", revoke_during_manifest)
    with pytest.raises(AuthenticationError):
        RuleReviewLedger(accounts, versioned=True).get(token, candidate["id"])


def test_worker_own_lease_expiry_arms_block_and_blocks_approval_but_not_rejection(setup, monkeypatch):
    import time
    from app.services import rule_validation_service

    accounts, service, admin, _, token, _, candidate = setup
    clock = {"now": time.time()}
    monkeypatch.setattr(rule_validation_service.time, "time", lambda: clock["now"])
    monkeypatch.setattr(rule_validation_service, "run_gold_checks", _passing)
    ledger = RuleReviewLedger(accounts, versioned=True)
    first = service.validate(token, candidate["id"])
    ledger.review(admin, candidate["id"], candidate["sha256"], "approve", "首次审核", expected_validation_id=first["id"])

    def expired_run(snapshot):
        # 金标运行期间时间越过租约期限，收尾事务必须发现并落闸
        clock["now"] += 601
        return _passing(snapshot)

    monkeypatch.setattr(rule_validation_service, "run_gold_checks", expired_run)
    with pytest.raises(RuntimeError, match="检查进程"):
        service.validate(token, candidate["id"])
    with accounts._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM rule_validation_results").fetchone()[0] == 1
        job_status = conn.execute("SELECT status FROM rule_validation_jobs ORDER BY rowid DESC LIMIT 1").fetchone()[0]
        control = conn.execute("SELECT blocked, reason, last_job_id FROM rule_validation_control WHERE id=1").fetchone()
    assert job_status == "abandoned"
    assert control["blocked"] == 1 and control["reason"] == "expired_job" and control["last_job_id"]
    with pytest.raises(RuntimeError, match="待检查"):
        service.validate(token, candidate["id"])
    with pytest.raises(RuntimeError, match="待检查"):
        ledger.review(admin, candidate["id"], candidate["sha256"], "approve", "过期任务必须先离线检查",
                      expected_validation_id=first["id"])
    assert ledger.review(admin, candidate["id"], candidate["sha256"], "reject", "退回仍可用")["decision"] == "reject"


def test_expired_finalization_with_failed_block_write_keeps_job_running(setup, monkeypatch):
    import time
    from app.services import rule_validation_service

    accounts, service, _, _, token, _, candidate = setup
    clock = {"now": time.time()}
    monkeypatch.setattr(rule_validation_service.time, "time", lambda: clock["now"])
    monkeypatch.setattr(rule_validation_service, "run_gold_checks", _passing)
    with accounts._connect() as conn:
        conn.execute("CREATE TRIGGER test_expired_block_failure BEFORE UPDATE ON rule_validation_control "
                     "WHEN NEW.blocked=1 AND OLD.blocked=0 BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END")

    def expired_run(snapshot):
        clock["now"] += 601
        return _passing(snapshot)

    monkeypatch.setattr(rule_validation_service, "run_gold_checks", expired_run)
    with pytest.raises(sqlite3.IntegrityError):
        service.validate(token, candidate["id"])
    with accounts._connect() as conn:
        row = conn.execute("SELECT status, lease_until FROM rule_validation_jobs "
                           "WHERE id=(SELECT id FROM rule_validation_jobs ORDER BY rowid DESC LIMIT 1)").fetchone()
        blocked = conn.execute("SELECT blocked FROM rule_validation_control WHERE id=1").fetchone()[0]
    assert row["status"] == "running" and row["lease_until"] <= clock["now"] and blocked == 0

    with accounts._connect() as conn:
        conn.execute("DROP TRIGGER test_expired_block_failure")
    with pytest.raises(RuntimeError, match="检查进程"):
        service.validate(token, candidate["id"])
    with accounts._connect() as conn:
        statuses = [r[0] for r in conn.execute("SELECT status FROM rule_validation_jobs ORDER BY rowid")]
        control = conn.execute("SELECT blocked, reason FROM rule_validation_control WHERE id=1").fetchone()
    assert statuses == ["abandoned"] and control["blocked"] == 1 and control["reason"] == "expired_job"


def test_concurrent_recycle_of_expired_job_prevents_result_recording(setup, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    import time
    from app.services import rule_validation_service

    accounts, service, _, _, token, _, candidate = setup
    clock = {"now": time.time()}
    monkeypatch.setattr(rule_validation_service.time, "time", lambda: clock["now"])
    entered, release = threading.Event(), threading.Event()

    def slow_expired_run(snapshot):
        entered.set()
        assert release.wait(timeout=20)
        clock["now"] += 601
        return _passing(snapshot)

    monkeypatch.setattr(rule_validation_service, "run_gold_checks", slow_expired_run)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(service.validate, token, candidate["id"])
        try:
            assert entered.wait(timeout=10)
            # 模拟另一进程 worker 的启动回收分支（与 validate 开头的过期回收语句一致）
            recycle_now = clock["now"] + 601
            with accounts._connect() as conn, conn:
                conn.execute("UPDATE rule_validation_jobs SET status='abandoned' WHERE status='running' AND lease_until<=?",
                             (recycle_now,))
                job_id = conn.execute("SELECT id FROM rule_validation_jobs WHERE status='abandoned' "
                                      "ORDER BY rowid DESC LIMIT 1").fetchone()[0]
                conn.execute("UPDATE rule_validation_control SET blocked=1,reason='expired_job',updated_at=?,last_job_id=? "
                             "WHERE id=1", (recycle_now, job_id))
        finally:
            release.set()
        with pytest.raises(ValueError, match="已变化"):
            future.result(timeout=10)
    with accounts._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM rule_validation_results").fetchone()[0] == 0
        control = conn.execute("SELECT blocked, reason, last_job_id FROM rule_validation_control WHERE id=1").fetchone()
    assert control["blocked"] == 1 and control["reason"] == "expired_job" and control["last_job_id"]
    with pytest.raises(RuntimeError, match="待检查"):
        service.validate(token, candidate["id"])


def test_expired_runner_exception_arms_control_at_lease_boundary(setup, monkeypatch):
    import time
    from app.services import rule_validation_service

    accounts, service, _, _, token, _, candidate = setup
    clock = {"now": time.time()}
    monkeypatch.setattr(rule_validation_service.time, "time", lambda: clock["now"])

    def expired_runner(snapshot):
        clock["now"] += 600
        raise ValueError("runner failed")

    monkeypatch.setattr(rule_validation_service, "run_gold_checks", expired_runner)
    with pytest.raises(ValueError, match="runner failed"):
        service.validate(token, candidate["id"])
    with accounts._connect() as conn:
        job = conn.execute("SELECT id, status FROM rule_validation_jobs ORDER BY rowid DESC LIMIT 1").fetchone()
        control = conn.execute("SELECT blocked, reason, last_job_id FROM rule_validation_control WHERE id=1").fetchone()
    assert job["status"] == "abandoned"
    assert (control["blocked"], control["reason"], control["last_job_id"]) == (1, "expired_job", job["id"])


def test_unexpired_runner_exception_does_not_arm_control(setup, monkeypatch):
    from app.services import rule_validation_service

    accounts, service, _, _, token, _, candidate = setup

    def failed_runner(snapshot):
        raise ValueError("runner failed")

    monkeypatch.setattr(rule_validation_service, "run_gold_checks", failed_runner)
    with pytest.raises(ValueError, match="runner failed"):
        service.validate(token, candidate["id"])
    with accounts._connect() as conn:
        assert conn.execute("SELECT status FROM rule_validation_jobs ORDER BY rowid DESC LIMIT 1").fetchone()[0] == "failed"
        assert conn.execute("SELECT blocked FROM rule_validation_control WHERE id=1").fetchone()[0] == 0


def test_existing_control_incident_keeps_original_evidence(setup):
    accounts, service, _, _, _, _, _ = setup
    with accounts._connect() as conn, conn:
        conn.execute("UPDATE rule_validation_control SET blocked=1, reason='first', updated_at=42, last_job_id='original' WHERE id=1")
        service._arm_control(conn, "expired_job", "later", 99)
        row = conn.execute("SELECT reason, updated_at, last_job_id FROM rule_validation_control WHERE id=1").fetchone()
    assert tuple(row) == ("first", 42, "original")


# ---- codex 三轮 P1-1：损坏库运行时初始化不得重置安全闸 ----

def test_partial_missing_tables_fail_closed_on_init(setup):
    """blocked=1 后控制表缺失（任务/结果仍在）→ 服务初始化 fail-closed，
    绝不把安全闸重建为可用。"""
    accounts, _, admin, _, _, _, _ = setup
    with accounts._connect() as conn:
        conn.execute("UPDATE rule_validation_control SET blocked=1, reason='synthetic incident' WHERE id=1")
        conn.execute("DROP TABLE rule_validation_control")
    with pytest.raises(RuntimeError, match="不完整"):
        RuleValidationService(accounts)
    # 阻断未被解除：任务表仍在、无新建控制行
    with accounts._connect() as conn:
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'rule_validation_%'")}
        assert "rule_validation_control" not in names


def test_missing_control_row_gets_defensive_block_on_restart(setup):
    """三表齐全但控制行丢失（数据损伤）→ 服务重启成功但保持阻断
    （防御性 blocked=1，绝不自动补成可用）。"""
    accounts, _, _, _, _, _, _ = setup
    with accounts._connect() as conn:
        conn.execute("DROP TRIGGER validation_control_no_delete")
        conn.execute("DELETE FROM rule_validation_control WHERE id=1")
    # 服务重启（两次构造 = 重启语义）
    RuleValidationService(accounts)
    RuleValidationService(accounts)
    with accounts._connect() as conn:
        row = conn.execute("SELECT blocked,reason FROM rule_validation_control WHERE id=1").fetchone()
        assert row["blocked"] == 1, "控制行缺失必须防御阻断"
        assert "防御阻断" in row["reason"]
    # 守卫确认：审核通过被阻断
    from app.services.rule_validation_guard import validation_blocked
    with accounts._connect() as conn:
        assert validation_blocked(conn) is True
