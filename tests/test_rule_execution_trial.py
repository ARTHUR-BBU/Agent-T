"""Explicit loopback-only database execution experiment; never production publication."""
import hashlib
import io
import time
import uuid
from threading import Event

import pytest
import yaml
from docx import Document
from fastapi.testclient import TestClient

from app.api import routes
from app.main import app
from app.services.account_store import AccountStore
from app.services.rule_snapshot import capture_rule_snapshot
from app.services.rule_versions import RuleVersionStore
from app.services.store import ReviewStore
from tests.helpers import wait_review_done


@pytest.fixture
def trial(tmp_path, monkeypatch):
    path = tmp_path / "accounts.sqlite"
    accounts = AccountStore(path)
    admin = accounts.bootstrap_admin("admin", "管理员", "testing-password-123")
    token = accounts.login("admin", "testing-password-123")
    base = capture_rule_snapshot("procurement")
    initial = RuleVersionStore(accounts).initialize(token, "procurement", base.config_bytes)
    config = base.configuration()
    next(item for item in config["items"] if item["id"] == "subject_matter")["enabled"] = False
    raw = yaml.safe_dump(config, allow_unicode=True).encode()
    new_id = uuid.uuid4().hex
    with accounts._connect() as conn:
        conn.execute("INSERT INTO rule_versions VALUES (?,?,?,?,?,?)", (new_id, "procurement", hashlib.sha256(raw).hexdigest(), raw, admin["id"], time.time()))
        conn.execute("UPDATE rule_active_versions SET version_id=?,generation=generation+1 WHERE category='procurement'", (new_id,))
    monkeypatch.setenv("ACCOUNT_LOCAL_ENABLED", "1")
    monkeypatch.setenv("ACCOUNT_DB_PATH", str(path))
    monkeypatch.setenv("RULE_EXECUTION_DB_TRIAL", "1")
    monkeypatch.setenv("PRECHECK_ENABLED", "false")
    monkeypatch.setenv("CATEGORY_SUGGEST_ENABLED", "false")
    monkeypatch.setattr(routes, "store", ReviewStore(str(tmp_path / "reviews.sqlite")))
    return accounts, initial, new_id, raw


def _client(peer="127.0.0.1"):
    return TestClient(app, base_url="http://127.0.0.1:8000", client=(peer, 12345))


def test_trial_upload_executes_database_snapshot_and_records_version(trial):
    _, _, version_id, raw = trial
    origin = {"Origin": "http://127.0.0.1:8000"}
    with _client() as client:
        categories = client.get("/api/categories")
        assert categories.status_code == 200 and categories.json()["rule_source"] == "database_test"
        assert [item["id"] for item in categories.json()["categories"]] == ["procurement"]
        login = client.post("/api/local-accounts/login", json={"username": "admin", "password": "testing-password-123"}, headers=origin)
        assert login.status_code == 200
        versions = client.get("/api/local-accounts/rules/versions", headers={**origin, "X-CSRF-Token": login.json()["csrf_token"]})
        assert versions.json()["effective_in_reviews"] is True and versions.json()["execution_scope"] == "local_test"
        catalog = client.get("/api/rules/catalog").json()
        item = next(item for item in catalog["packs"][0]["items"] if item["id"] == "subject_matter")
        assert item["enabled"] is False
        response = client.post("/api/upload", data={"category": "procurement", "pasted_text": "采购合同。甲方采购乙方设备。"}, headers=origin)
        assert response.status_code == 200, response.text
        review = wait_review_done(client, response.json()["review_id"])
        assert review["status"] == "done"
        assert "subject_matter" not in {item["id"] for item in review["items"]}
        assert review["rule_provenance"] == {"source": "database_test", "category": "procurement",
                                               "content_sha256": hashlib.sha256(raw).hexdigest(),
                                               "version_id": version_id, "generation": 2}
        report = client.get(f"/api/review/{review['id']}/report")
        assert report.status_code == 200
        text = "\n".join(p.text for p in Document(io.BytesIO(report.content)).paragraphs)
        assert "数据库试验版本" in text and version_id in text and hashlib.sha256(raw).hexdigest() in text
        assert client.get("/api/rule-drafts/capability").status_code == 404
        assert client.post("/api/upload", data={"category": "procurement", "pasted_text": "采购合同", "force": "1"}, headers=origin).status_code == 409


def test_trial_fails_closed_on_bad_database_and_non_loopback(trial, monkeypatch):
    accounts, *_ = trial
    with _client("203.0.113.5") as remote:
        assert remote.get("/api/reviews").status_code == 403
        assert remote.get("/").status_code == 403
    with _client() as client:
        assert client.get("/api/categories", headers={"X-Forwarded-For": "127.0.0.1"}).status_code == 403
        assert client.post("/api/upload", data={"pasted_text": "合同"}).status_code == 403
        assert client.post("/api/review/unknown/items/item/revision", headers={"Origin": "http://127.0.0.1:9999", "Sec-Fetch-Site": "same-site"}).status_code == 403
    with accounts._connect() as conn:
        conn.execute("DROP TRIGGER rule_versions_no_update")
        conn.execute("UPDATE rule_versions SET content=? WHERE id=(SELECT version_id FROM rule_active_versions)", (b"corrupted",))
    with _client() as client:
        assert client.get("/api/categories").status_code == 503
        assert client.post("/api/upload", data={"pasted_text": "合同"}, headers={"Origin": "http://127.0.0.1:8000"}).status_code == 503
    monkeypatch.setenv("ACCOUNT_DB_PATH", str(accounts.path.parent / "missing.sqlite"))
    with _client() as client:
        assert client.get("/").status_code == 503


def test_trial_bad_flag_and_disabled_account_mode_fail_closed(trial, monkeypatch):
    monkeypatch.setenv("RULE_EXECUTION_DB_TRIAL", "unexpected")
    with _client() as client:
        response = client.get("/api/categories")
        assert response.status_code == 503 and response.headers["X-Rule-Execution-Trial"] == "invalid"
    monkeypatch.setenv("RULE_EXECUTION_DB_TRIAL", "1")
    monkeypatch.setenv("ACCOUNT_LOCAL_ENABLED", "0")
    with _client() as client:
        response = client.get("/api/categories")
        assert response.status_code == 503 and response.headers["X-Rule-Execution-Trial"] == "database_test"


def test_inflight_review_keeps_captured_version_after_pointer_changes(trial, monkeypatch):
    accounts, initial, old_id, _ = trial
    started, release = Event(), Event()
    real_run = routes.run_review
    first = [True]

    def delayed(*args, **kwargs):
        if first[0]:
            first[0] = False
            started.set()
            assert release.wait(10)
        return real_run(*args, **kwargs)

    monkeypatch.setattr(routes, "run_review", delayed)
    origin = {"Origin": "http://127.0.0.1:8000"}
    with _client() as client:
        first_response = client.post("/api/upload", data={"category": "procurement", "pasted_text": "采购合同。甲方购买设备。"}, headers=origin)
        assert first_response.status_code == 200
        assert started.wait(5)
        try:
            with accounts._connect() as conn:
                conn.execute("UPDATE rule_active_versions SET version_id=?,generation=generation+1 WHERE category='procurement'", (initial["id"],))
        finally:
            release.set()
        old = wait_review_done(client, first_response.json()["review_id"])
        second_response = client.post("/api/upload", data={"category": "procurement", "pasted_text": "采购合同。甲方购买设备。"}, headers=origin)
        assert second_response.status_code == 200
        new = wait_review_done(client, second_response.json()["review_id"])
    assert old["rule_provenance"]["version_id"] == old_id and old["rule_provenance"]["generation"] == 2
    assert new["rule_provenance"]["version_id"] == initial["id"] and new["rule_provenance"]["generation"] == 3
    assert "subject_matter" not in {item["id"] for item in old["items"]}
    assert "subject_matter" in {item["id"] for item in new["items"]}


def test_trial_confirmation_binds_document_version_and_is_single_use(trial, monkeypatch):
    from app.services.precheck import PrecheckOutcome, PrecheckResult

    accounts, initial, _, _ = trial
    token = accounts.login("admin", "testing-password-123")
    lease = capture_rule_snapshot("lease")
    RuleVersionStore(accounts).initialize(token, "lease", lease.config_bytes)
    outcome = PrecheckOutcome(performed=True, result=PrecheckResult(detected_type="租赁合同", is_supported=True,
                              suggested_category="lease", confidence="high", summary="需要确认"))
    monkeypatch.setattr(routes.precheck_service, "run_precheck", lambda *args: outcome)
    origin = {"Origin": "http://127.0.0.1:8000"}
    body = {"category": "procurement", "stance": "neutral", "pasted_text": "合同内容，用于测试。"}
    with _client() as client:
        def confirmation():
            response = client.post("/api/upload", data=body, headers=origin)
            assert response.status_code == 200 and response.json()["status"] == "category_confirm"
            return response.json()["confirmation_token"]

        ticket = confirmation()
        altered = {**body, "pasted_text": "不同合同", "force": "1", "confirmation_token": ticket}
        assert client.post("/api/upload", data=altered, headers=origin).status_code == 409
        assert client.post("/api/upload", data={**body, "force": "1", "confirmation_token": ticket}, headers=origin).status_code == 409
        stale = confirmation()
        with accounts._connect() as conn:
            conn.execute("UPDATE rule_active_versions SET version_id=?,generation=generation+1 WHERE category='procurement'", (initial["id"],))
        assert client.post("/api/upload", data={**body, "force": "1", "confirmation_token": stale}, headers=origin).status_code == 409
        valid = confirmation()
        response = client.post("/api/upload", data={**body, "category": "lease", "force": "1", "confirmation_token": valid}, headers=origin)
        assert response.status_code == 200 and response.json()["status"] == "uploaded"
        assert wait_review_done(client, response.json()["review_id"])["status"] == "done"
        assert client.post("/api/upload", data={**body, "category": "lease", "force": "1", "confirmation_token": valid}, headers=origin).status_code == 409
