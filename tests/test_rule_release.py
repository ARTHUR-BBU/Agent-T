"""Atomic release requires reviewed gold evidence and a separate full-regression record."""
import hashlib
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.services.account_store import AccountStore, AuthorizationError
from app.services.rule_candidate_validation import GoldCheckResult
from app.services.rule_release import RuleReleaseService
from app.services.rule_review_ledger import RuleReviewLedger
from app.services.rule_snapshot import capture_rule_snapshot
from app.services.rule_submission import RuleSubmissionService
from app.services.rule_validation_service import RuleValidationService
from app.services.rule_versions import RuleVersionStore
from tests.helpers import create_active_account


def test_full_manifest_detects_test_configuration_changes(tmp_path, monkeypatch):
    from app.services import rule_release

    monkeypatch.setattr(rule_release, "_ROOT", tmp_path)
    config = tmp_path / "pytest.ini"
    config.write_text("[pytest]\naddopts = -q\n", encoding="utf-8")
    before = rule_release.current_full_manifest()
    config.write_text("[pytest]\naddopts = -k does_not_exist\n", encoding="utf-8")
    assert rule_release.current_full_manifest() != before


def test_full_manifest_covers_binary_fixtures_scripts_tools_and_removals(tmp_path, monkeypatch):
    from app.services import rule_release

    monkeypatch.setattr(rule_release, "_ROOT", tmp_path)
    for directory in ("fixtures-real", "scripts", "tools"):
        (tmp_path / directory).mkdir()
    files = [tmp_path / "fixtures-real" / "contract.docx", tmp_path / "scripts" / "runner.ps1",
             tmp_path / "tools" / "baseline.dat"]
    for path in files:
        before = rule_release.current_full_manifest()
        path.write_bytes(b"\x00original\xff")
        added = rule_release.current_full_manifest()
        assert added != before
        path.write_bytes(b"\x00changed\xff")
        assert rule_release.current_full_manifest() != added
    before = rule_release.current_full_manifest()
    files[0].unlink()
    assert rule_release.current_full_manifest() != before


def test_full_manifest_ignores_generated_caches_and_rejects_links(tmp_path, monkeypatch):
    from pathlib import Path
    from app.services import rule_release

    monkeypatch.setattr(rule_release, "_ROOT", tmp_path)
    cache = tmp_path / "tests" / "__pycache__"
    cache.mkdir(parents=True)
    before = rule_release.current_full_manifest()
    (cache / "generated.pyc").write_bytes(b"runtime cache")
    assert rule_release.current_full_manifest() == before
    link = tmp_path / "tests" / "external.txt"
    link.write_text("not a real link", encoding="utf-8")
    real_is_symlink = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda path: path == link or real_is_symlink(path))
    with pytest.raises(RuntimeError, match="不能是链接"):
        rule_release.current_full_manifest()


def test_full_manifest_rejects_dangling_root_link(tmp_path, monkeypatch):
    from pathlib import Path
    from app.services import rule_release

    monkeypatch.setattr(rule_release, "_ROOT", tmp_path)
    dangling = tmp_path / "fixtures-real"
    real_is_symlink = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda path: path == dangling or real_is_symlink(path))
    assert not dangling.exists()
    with pytest.raises(RuntimeError, match="不能是链接"):
        rule_release.current_full_manifest()


def test_release_manifest_rejects_oversized_inputs(tmp_path, monkeypatch):
    from app.services import rule_release

    monkeypatch.setattr(rule_release, "_ROOT", tmp_path)
    monkeypatch.setattr(rule_release, "_MANIFEST_MAX_BYTES", 3)
    (tmp_path / "pyproject.toml").write_bytes(b"1234")
    with pytest.raises(RuntimeError, match="大小超限"):
        rule_release.current_full_manifest()


@pytest.fixture
def setup_release(tmp_path, monkeypatch):
    from app.services import rule_release, rule_review_ledger, rule_validation_service

    monkeypatch.setattr(rule_review_ledger, "current_input_manifest", lambda snapshot: "a" * 64)
    monkeypatch.setattr(rule_release, "current_input_manifest", lambda snapshot: "a" * 64)
    monkeypatch.setattr(rule_release, "current_full_manifest", lambda: "c" * 64)
    accounts = AccountStore(tmp_path / "accounts.sqlite")
    admin = accounts.bootstrap_admin("admin", "管理员", "testing-password-123")
    admin_token = accounts.login("admin", "testing-password-123")
    create_active_account(accounts, admin_token, "alice", "用户", "alice-password-123", "user")
    token = accounts.login("alice", "alice-password-123")
    snapshot = capture_rule_snapshot("procurement")
    initial = RuleVersionStore(accounts).initialize(admin_token, "procurement", snapshot.config_bytes)
    draft = {"category": "procurement", "operation": "add", "item_id": "custom_delivery",
             "name": "交付清单", "description": "检查交付清单", "reason": "明确交付范围",
             "segment": "B", "literal": "交付清单", "positive": "依照交付清单验收",
             "negative": "仅安排验收", "base_sha256": hashlib.sha256(snapshot.config_bytes).hexdigest()}
    candidate = RuleSubmissionService(accounts).submit(token, draft, expected_generation=1, request_id=uuid.uuid4().hex)

    def runner(candidate_snapshot):
        return GoldCheckResult(category=candidate_snapshot.category, candidate_sha256=hashlib.sha256(candidate_snapshot.config_bytes).hexdigest(),
                               input_manifest_sha256="a" * 64, suite="tests/gold.py", passed=True,
                               passed_tests=3, collected_tests=3, skipped_tests=0, failed_test_ids=(),
                               exit_code=0, output_sha256="b" * 64)

    monkeypatch.setattr(rule_validation_service, "run_gold_checks", runner)
    validation = RuleValidationService(accounts).validate(token, candidate["id"])
    review = RuleReviewLedger(accounts, versioned=True).review(admin_token, candidate["id"], candidate["sha256"],
                                                               "approve", "审核通过", expected_validation_id=validation["id"])
    return accounts, RuleReleaseService(accounts), admin, admin_token, token, initial, candidate, validation, review


def _insert_synthetic_full_result(accounts, candidate):
    """Test-only fixture; no public API can create this evidence."""
    result_id = uuid.uuid4().hex
    with accounts._connect() as conn:
        conn.execute("INSERT INTO rule_full_regression_results VALUES (?,?,?,?,?,?,?,?)",
                     (result_id, candidate["id"], candidate["sha256"], candidate["base_generation"],
                      "c" * 64, 1, "complete_candidate_regression", time.time()))
    return result_id


def test_release_requires_full_regression_and_authorized_actor(setup_release):
    accounts, service, _, admin, user, initial, candidate, validation, review = setup_release
    with pytest.raises(ValueError, match="完整回归"):
        service.publish(admin, candidate["id"], expected_generation=1, validation_id=validation["id"], review_id=review["id"],
                        full_result_id="missing", request_id=uuid.uuid4().hex)
    full = _insert_synthetic_full_result(accounts, candidate)
    with pytest.raises(AuthorizationError):
        service.publish(user, candidate["id"], expected_generation=1, validation_id=validation["id"], review_id=review["id"],
                        full_result_id=full, request_id=uuid.uuid4().hex)
    published = service.publish(admin, candidate["id"], expected_generation=1, validation_id=validation["id"],
                                review_id=review["id"], full_result_id=full, request_id=uuid.uuid4().hex)
    assert published["generation"] == 2 and published["previous_version_id"] == initial["id"]
    assert hashlib.sha256(RuleVersionStore(accounts).current("procurement").config_bytes).hexdigest() == candidate["sha256"]


@pytest.mark.parametrize("condition", ["blocked", "running"])
def test_publish_rejects_unresolved_validation_incident(setup_release, condition):
    accounts, service, _, admin, _, _, candidate, validation, review = setup_release
    full = _insert_synthetic_full_result(accounts, candidate)
    with accounts._connect() as conn:
        if condition == "blocked":
            conn.execute("UPDATE rule_validation_control SET blocked=1,reason='uncertain_process',updated_at=? WHERE id=1", (time.time(),))
        else:
            conn.execute("INSERT INTO rule_validation_jobs VALUES (?,?,?,?,?,?,?)",
                         ("other-running", candidate["id"], candidate["submitted_by"], "running", 1, 9999999999, None))
    with pytest.raises(RuntimeError, match="待检查"):
        service.publish(admin, candidate["id"], expected_generation=1, validation_id=validation["id"],
                        review_id=review["id"], full_result_id=full, request_id=uuid.uuid4().hex)


def test_concurrent_release_has_one_winner_and_rollback_creates_new_generation(setup_release):
    accounts, service, _, admin, _, initial, candidate, validation, review = setup_release
    full = _insert_synthetic_full_result(accounts, candidate)

    def publish(_):
        try:
            return service.publish(admin, candidate["id"], expected_generation=1, validation_id=validation["id"],
                                   review_id=review["id"], full_result_id=full, request_id=uuid.uuid4().hex)
        except ValueError:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(publish, range(2)))
    assert sum(result is not None for result in results) == 1
    restored = service.rollback(admin, "procurement", initial["id"], expected_generation=2,
                                reason="恢复上一版本", request_id=uuid.uuid4().hex)
    assert restored["generation"] == 3 and restored["version_id"] == initial["id"]
    with accounts._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM rule_release_events").fetchone()[0] == 2
