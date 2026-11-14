"""Local release API gates; full-regression records here are synthetic test fixtures."""
import uuid

from fastapi.testclient import TestClient

from app.main import app
from tests.test_rule_release import setup_release, _insert_synthetic_full_result  # noqa: F401


def test_local_release_permissions_evidence_history_and_rollback(setup_release, monkeypatch):  # noqa: F811
    from app.services import owned_review_maintenance

    accounts, _, _, _, _, initial, candidate, validation, review = setup_release
    monkeypatch.setenv("ACCOUNT_LOCAL_ENABLED", "1")
    monkeypatch.setenv("ACCOUNT_DB_PATH", str(accounts.path))
    monkeypatch.setattr(owned_review_maintenance, "enabled", lambda: True)
    origin = {"Origin": "http://127.0.0.1:8000"}
    with TestClient(app, base_url="http://127.0.0.1:8000", client=("127.0.0.1", 12345)) as admin, \
         TestClient(app, base_url="http://127.0.0.1:8000", client=("127.0.0.1", 12345)) as user:
        def login(client, name, password):
            response = client.post("/api/local-accounts/login", json={"username": name, "password": password}, headers=origin)
            assert response.status_code == 200
            return {**origin, "X-CSRF-Token": response.json()["csrf_token"]}

        ah = login(admin, "admin", "testing-password-123")
        uh = login(user, "alice", "alice-password-123")
        root = "/api/local-accounts/rules"
        monkeypatch.delenv("RULE_RELEASE_LOCAL_TRIAL", raising=False)
        assert admin.get(root + "/release-history", headers=ah).status_code == 404
        monkeypatch.setenv("RULE_RELEASE_LOCAL_TRIAL", "1")
        assert user.get(root + "/release-history", headers=uh).status_code == 403
        assert admin.get(root + "/release-history", headers=origin).status_code == 403
        body = {"candidate_sha256": candidate["sha256"], "expected_generation": 1,
                "validation_id": validation["id"], "review_id": review["id"],
                "full_result_id": uuid.uuid4().hex, "request_id": uuid.uuid4().hex}
        url = root + "/candidates/" + candidate["id"] + "/publish"
        assert user.post(url, json=body, headers=uh).status_code == 403
        missing = admin.post(url, json=body, headers=ah)
        assert missing.status_code == 422 and "完整回归" in missing.json()["detail"]
        synthetic = _insert_synthetic_full_result(accounts, candidate)
        body["full_result_id"] = synthetic
        assert admin.post(url, json={**body, "candidate_sha256": "0" * 64}, headers=ah).status_code == 422
        published = admin.post(url, json=body, headers=ah)
        assert published.status_code == 200 and published.json()["generation"] == 2
        assert admin.post(url, json=body, headers=ah).json() == published.json()
        history = admin.get(root + "/release-history", headers=ah).json()
        assert history["scope"] == "local_test" and history["events"][0]["username"] == "admin"
        assert initial["id"] in {version["id"] for version in history["versions"]}
        rollback = {"category": "procurement", "version_id": initial["id"], "expected_generation": 2,
                    "reason": "恢复旧规则", "request_id": uuid.uuid4().hex}
        assert user.post(root + "/rollback", json=rollback, headers=uh).status_code == 403
        assert admin.post(root + "/rollback", json=rollback, headers=origin).status_code == 403
        result = admin.post(root + "/rollback", json=rollback, headers=ah)
        assert result.status_code == 200 and result.json()["generation"] == 3
        assert admin.post(root + "/rollback", json=rollback, headers=ah).json() == result.json()
        assert admin.post(root + "/rollback", json={**rollback, "request_id": uuid.uuid4().hex}, headers=ah).status_code == 422
