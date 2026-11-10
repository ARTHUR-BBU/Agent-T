"""Account-mode HTTP integration in a subprocess; model calls are synthetic."""
import json
import os
import subprocess
import sys

from app.services.account_store import AccountStore
from app.services.owned_review_store import OwnedReviewStore
from app.services.rule_snapshot import capture_rule_snapshot
from app.services.rule_versions import RuleVersionStore
from app.services.store import ReviewStore
from tests.helpers import create_active_account


def test_account_http_confirm_and_review_in_real_mode(tmp_path):
    path = tmp_path / "accounts.sqlite"
    shared = tmp_path / "shared.sqlite"
    accounts = AccountStore(path)
    accounts.bootstrap_admin("admin", "管理员", "testing-password-123")
    admin = accounts.login("admin", "testing-password-123")
    create_active_account(accounts, admin, "alice", "甲", "alice-password-123", "user")
    create_active_account(accounts, admin, "bobby", "乙", "bobby-password-123", "user")
    OwnedReviewStore(accounts, ReviewStore(str(path)))
    versions = RuleVersionStore(accounts)
    for category in ("procurement", "lease"):
        versions.initialize(admin, category, capture_rule_snapshot(category).config_bytes)
    env = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP",
                                              "APPDATA", "LOCALAPPDATA", "USERPROFILE") if key in os.environ}
    env.update(OWNED_LLM_EXECUTION="1", ACCOUNT_LOCAL_ENABLED="1", ACCOUNT_DB_PATH=str(path), STORE_DB_PATH=str(shared),
               RULE_EXECUTION_DB_TRIAL="1", CONTRACT_ACCOUNT_READ_TRIAL="1", CONTRACT_ACCOUNT_UPLOAD_TRIAL="1",
               PRECHECK_ENABLED="true", CATEGORY_SUGGEST_ENABLED="false", QUALITY_ENABLED="false", OBJECTIONS_ENABLED="false",
               RATE_LIMIT_UPLOAD_PER_MINUTE="0", PYTHONIOENCODING="utf-8")
    script = r'''
import json, time
from fastapi.testclient import TestClient
from app.main import app
from app.services import precheck
from app.services.account_store import AccountStore
from app.api.routes_local_owned_upload import _services
from pathlib import Path
import os
accounts = AccountStore(Path(os.environ["ACCOUNT_DB_PATH"]))
old_token = accounts.login("alice", "alice-password-123")
jobs, ledger, _ = _services(accounts, accounts.path)
orphan_rid, orphan_job, orphan_secret = jobs.create(old_token, filename="撤权任务", category="procurement")
orphan_call = ledger.begin(old_token, orphan_job, orphan_secret, node="quality", provider="zhipu", model="glm-5.2")
accounts.logout(old_token)
precheck.run_precheck = lambda *args, **kwargs: precheck.PrecheckOutcome(performed=True, result=precheck.PrecheckResult(
    detected_type="租赁合同", is_supported=True, suggested_category="lease", confidence="high", summary="请确认"))
def client(): return TestClient(app, base_url="http://127.0.0.1:8000", client=("127.0.0.1", 12345))
origin = {"Origin": "http://127.0.0.1:8000"}
with client() as alice, client() as bob:
    for _ in range(100):
        with accounts._connect() as conn:
            state = conn.execute("SELECT status FROM owned_review_jobs WHERE id=?", (orphan_job,)).fetchone()[0]
            outcome = conn.execute("SELECT outcome FROM owned_llm_attempts WHERE id=?", (orphan_call,)).fetchone()[0]
        if state == "failed": break
        time.sleep(.05)
    assert state == "failed" and outcome == "unknown"
    a = alice.post("/api/local-accounts/login", json={"username":"alice", "password":"alice-password-123"}, headers=origin)
    b = bob.post("/api/local-accounts/login", json={"username":"bobby", "password":"bobby-password-123"}, headers=origin)
    assert a.status_code == b.status_code == 200
    ah = {**origin, "X-CSRF-Token": a.json()["csrf_token"]}
    bh = {**origin, "X-CSRF-Token": b.json()["csrf_token"]}
    url = "/api/local-accounts/contracts/upload"
    body = {"pasted_text":"虚构租赁合同：甲方向乙方租用场地。", "category":"procurement", "stance":"neutral"}
    assert alice.post(url, data=body, headers=origin).status_code == 403
    response = alice.post(url, data=body, headers=ah)
    assert response.status_code == 200, response.text
    initial = response.json()
    assert initial["status"] == "category_confirm" and initial["suggested_category"] == "lease"
    assert "secret" not in initial and response.headers["cache-control"] == "no-store"
    confirmation = {"pasted_text":body["pasted_text"], "category":"lease", "confirmation_token":initial["confirmation_token"]}
    assert bob.post(url, data=confirmation, headers=bh).status_code == 403
    wrong = {**confirmation, "pasted_text":"更改合同内容"}
    assert alice.post(url, data=wrong, headers=ah).status_code == 403
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=2) as pool:
        attempts = list(pool.map(lambda _: alice.post(url, data=confirmation, headers=ah), range(2)))
    assert sorted(item.status_code for item in attempts) == [200, 403]
    accepted = next(item for item in attempts if item.status_code == 200)
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["status"] == "uploaded" and accepted.json()["review_id"] == initial["review_id"]
    assert alice.post(url, data=confirmation, headers=ah).status_code == 403
    detail = "/api/local-accounts/contracts/" + initial["review_id"]
    for _ in range(150):
        result = alice.get(detail, headers=ah)
        assert result.status_code == 200, result.text
        if result.json()["status"] in ("done", "error"): break
        time.sleep(.1)
    assert result.json()["status"] == "done", result.text
    assert result.json()["category"] == "lease" and result.json()["rule_provenance"]["source"] == "database_test"
    assert bob.get(detail, headers=bh).status_code == 404
    assert alice.get("/api/reviews").status_code == 404
    assert alice.post("/api/upload", data=body, headers=ah).status_code == 404
    precheck.run_precheck = lambda *args, **kwargs: precheck.PrecheckOutcome(skip_reason="disabled")
    direct = bob.post(url, data={"pasted_text":"虚构采购合同：购买设备。", "category":"procurement", "stance":"neutral"}, headers=bh)
    assert direct.status_code == 200 and direct.json()["status"] == "uploaded", direct.text
    bob_detail = "/api/local-accounts/contracts/" + direct.json()["review_id"]
    for _ in range(150):
        bob_result = bob.get(bob_detail, headers=bh)
        if bob_result.json()["status"] in ("done", "error"): break
        time.sleep(.1)
    assert bob_result.json()["status"] == "done", bob_result.text
    assert bob_result.json()["precheck"]["performed"] is False
    assert bob_result.json()["precheck"]["skip_reason"] == "disabled"
    assert alice.get(bob_detail, headers=ah).status_code == 404
    from app.api import routes_local_owned_upload as upload
    def fail_start(*args): raise RuntimeError("synthetic failure")
    upload._start = fail_start
    before = bob.get("/api/local-accounts/contracts", headers=bh)
    before_ids = {row["id"] for row in before.json()["reviews"]}
    failed = bob.post(url, data={"pasted_text":"虚构采购合同：测试失败。", "category":"procurement", "stance":"neutral"}, headers=bh)
    assert failed.status_code == 503
    listing = bob.get("/api/local-accounts/contracts", headers=bh)
    created = [row for row in listing.json()["reviews"] if row["id"] not in before_ids]
    assert listing.status_code == 200 and len(created) == 1 and created[0]["status"] == "error"
print(json.dumps({"ok": True}))
'''
    result = subprocess.run([sys.executable, "-X", "utf8", "-c", script], env=env, capture_output=True,
                            text=True, encoding="utf-8", timeout=90)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip()) == {"ok": True}
    assert not shared.exists()
