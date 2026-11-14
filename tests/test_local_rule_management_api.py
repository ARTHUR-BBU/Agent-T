"""Local rule candidates are submitted and reviewed as authenticated accounts."""
import uuid

from fastapi.testclient import TestClient

from app.main import app
from app.services.account_store import AccountStore
from app.services.rule_candidate_validation import GoldCheckResult
from tests.helpers import create_active_account


def _client():
    return TestClient(app, base_url="http://127.0.0.1:8000", client=("127.0.0.1", 12345))


def test_account_bound_candidate_and_review_flow(tmp_path, monkeypatch):
    from app.services import rule_review_ledger, rule_validation_service

    monkeypatch.setenv("ACCOUNT_LOCAL_ENABLED", "1")
    monkeypatch.setenv("ACCOUNT_DB_PATH", str(tmp_path / "accounts.sqlite"))
    store = AccountStore(tmp_path / "accounts.sqlite")
    store.bootstrap_admin("admin", "管理员", "testing-password-123")
    admin_token = store.login("admin", "testing-password-123")
    alice = create_active_account(store, admin_token, "alice", "甲", "alice-password-123", "user")
    create_active_account(store, admin_token, "bob", "乙", "bobby-password-123", "user")

    def runner(snapshot):
        import hashlib
        return GoldCheckResult(category=snapshot.category, candidate_sha256=hashlib.sha256(snapshot.config_bytes).hexdigest(),
                               input_manifest_sha256="a" * 64, suite="tests/gold.py", passed=True,
                               passed_tests=3, collected_tests=3, skipped_tests=0, failed_test_ids=(),
                               exit_code=0, output_sha256="b" * 64)

    monkeypatch.setattr(rule_validation_service, "run_gold_checks", runner)
    monkeypatch.setattr(rule_review_ledger, "current_input_manifest", lambda snapshot: "a" * 64)
    origin = {"Origin": "http://127.0.0.1:8000"}
    with _client() as admin, _client() as user, _client() as other:
        def login(client, name, password):
            response = client.post("/api/local-accounts/login", json={"username": name, "password": password}, headers=origin)
            assert response.status_code == 200
            return {**origin, "X-CSRF-Token": response.json()["csrf_token"]}

        ah = login(admin, "admin", "testing-password-123")
        uh = login(user, "alice", "alice-password-123")
        oh = login(other, "bob", "bobby-password-123")
        url = "/api/local-accounts/rules"
        assert user.post(url + "/initialize", json={"category": "procurement"}, headers=uh).status_code == 403
        assert admin.post(url + "/initialize", json={"category": "procurement"}, headers=origin).status_code == 403
        initialized = admin.post(url + "/initialize", json={"category": "procurement"}, headers=ah)
        assert initialized.status_code == 200
        version = initialized.json()
        catalog = user.get(url + "/versions", headers=uh)
        assert catalog.status_code == 200 and catalog.json()["effective_in_reviews"] is False
        assert catalog.json()["versions"][0]["generation"] == 1
        draft = {"category": "procurement", "operation": "add", "item_id": "custom_delivery",
                 "name": "交付清单", "description": "检查交付清单", "reason": "明确交付范围",
                 "segment": "B", "literal": "交付清单", "positive": "依照交付清单验收",
                 "negative": "仅安排验收", "base_sha256": version["sha256"]}
        payload = {"draft": draft, "expected_generation": 1, "request_id": uuid.uuid4().hex,
                   "examples_acknowledged": True}
        assert user.post(url + "/candidates", json={**payload, "submitted_by": "forged"}, headers=uh).status_code == 422
        assert user.post(url + "/candidates", json={**payload, "examples_acknowledged": False}, headers=uh).status_code == 422
        response = user.post(url + "/candidates", json=payload, headers=uh)
        assert response.status_code == 200, response.text
        candidate = response.json()
        assert candidate["submitted_by"] == alice["id"]
        assert len(user.get(url + "/candidates", headers=uh).json()["candidates"]) == 1
        assert other.get(url + "/candidates", headers=oh).json()["candidates"] == []
        second = user.post(url + "/candidates", json={**payload, "request_id": uuid.uuid4().hex,
                           "draft": {**draft, "item_id": "custom_delivery_two"}}, headers=uh)
        assert second.status_code == 200
        first_page = user.get(url + "/candidates?limit=1", headers=uh).json()
        assert first_page["has_more"] is True and len(first_page["next_before"]) == 32
        assert user.get(url + "/candidates?before=999999999999999999999999999", headers=uh).status_code == 422
        assert other.get(url + f"/candidates?before={first_page['next_before']}", headers=oh).status_code == 422
        third = user.post(url + "/candidates", json={**payload, "request_id": uuid.uuid4().hex,
                          "draft": {**draft, "item_id": "custom_delivery_three"}}, headers=uh)
        assert third.status_code == 200
        next_page = user.get(url + f"/candidates?limit=1&before={first_page['next_before']}", headers=uh).json()
        assert next_page["has_more"] is False
        assert first_page["candidates"][0]["id"] != next_page["candidates"][0]["id"]
        detail_url = url + "/candidates/" + candidate["id"]
        assert other.get(detail_url, headers=oh).status_code == 403
        assert user.get(detail_url, headers=uh).json()["status"] == "待测试"
        review = {"candidate_sha256": candidate["sha256"], "decision": "approve", "reason": "人工审核"}
        assert admin.post(detail_url + "/review", json=review, headers=ah).status_code == 422
        assert user.post(detail_url + "/review", json=review, headers=uh).status_code == 403
        tested = user.post(detail_url + "/validate", headers=uh)
        assert tested.status_code == 200 and tested.json()["passed"] is True
        approved = admin.post(detail_url + "/review", json={**review, "expected_validation_id": tested.json()["id"]}, headers=ah)
        assert approved.status_code == 200 and approved.json()["validation_id"] == tested.json()["id"]
        reviewed = user.get(detail_url, headers=uh).json()
        assert reviewed["status"] == "已审核"
        assert reviewed["submitted_by_display_name"] == "甲"
        assert reviewed["review"]["reviewer_display_name"] == "管理员"
