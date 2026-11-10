"""Account-bound candidate UI shows its local, non-publishing scope."""
import json
import os
import subprocess
import sys
import time
import urllib.request

import pytest

pytest.importorskip("playwright")
from tests.test_frontend_e2e import base_url, desktop_page, pw_browser  # noqa: E402,F401
from tests.test_frontend_e2e import _free_port  # noqa: E402


def test_trial_warning_and_confirmation_token_are_visible_and_sent(desktop_page, base_url):  # noqa: F811
    page = desktop_page
    requests = []
    categories = [{"id": "procurement", "label": "采购合同", "stances": {"allowed": ["neutral"], "labels": {"neutral": "中性"}}},
                  {"id": "lease", "label": "租赁合同", "stances": {"allowed": ["neutral"], "labels": {"neutral": "中性"}}}]
    page.route("**/api/categories", lambda route: route.fulfill(
        status=200, content_type="application/json", headers={"X-Rule-Execution-Trial": "database_test"},
        body=json.dumps({"rule_source": "database_test", "categories": categories}, ensure_ascii=False)))

    def upload(route):
        requests.append(route.request.post_data or "")
        if len(requests) == 1:
            response = {"status": "category_confirm", "suggested_category": "lease", "supported_categories": categories,
                        "confirmation_token": "trial-token", "precheck": {"detected_type": "租赁合同", "summary": "需确认"}}
            route.fulfill(status=200, content_type="application/json", body=json.dumps(response, ensure_ascii=False))
        else:
            route.fulfill(status=200, content_type="application/json", body=json.dumps({
                "status": "category_confirm", "suggested_category": None,
                "precheck": {"detected_type": "测试", "summary": "测试已收到确认"}}, ensure_ascii=False))

    page.route("**/api/upload", upload)
    page.goto(base_url, wait_until="domcontentloaded")
    page.locator("#rule-execution-trial-warning").wait_for(state="visible")
    assert "尚未按账户隔离" in page.locator("#rule-execution-trial-warning").inner_text()
    page.locator("#entry-paste").click()
    page.locator("#paste-text").fill("合同文本测试")
    page.locator("#btn-upload").click()
    page.locator("#precheck-dialog").wait_for(state="visible")
    page.locator("#btn-precheck-switch").click()
    page.get_by_text("测试已收到确认").wait_for()
    assert len(requests) == 2 and "confirmation_token" in requests[1] and "trial-token" in requests[1]


def test_category_fetch_invalid_response_disables_upload(desktop_page, base_url):  # noqa: F811
    page = desktop_page
    page.route("**/api/categories", lambda route: route.fulfill(
        status=200, content_type="application/json", headers={"X-Rule-Execution-Trial": "database_test"}, body="invalid-json"))
    page.goto(base_url, wait_until="domcontentloaded")
    page.locator("#rule-execution-trial-warning").wait_for(state="visible")
    assert "尚未按账户隔离" in page.locator("#rule-execution-trial-warning").inner_text()
    page.locator("#entry-paste").click()
    page.locator("#paste-text").fill("合同测试")
    assert page.locator("#category").is_disabled()
    assert page.locator("#btn-upload").is_disabled()
    page.locator("#entry-file").click()
    assert page.locator("#btn-upload").is_disabled()


def test_upload_availability_and_server_category_list(desktop_page, base_url):  # noqa: F811
    page = desktop_page
    pending = []
    page.route("**/api/categories", lambda route: route.fulfill(
        status=200, content_type="application/json", body=json.dumps({"categories": [
            {"id": "lease", "label": "新租赁名称", "stances": {"allowed": ["neutral"], "labels": {"neutral": "中性"}}}
        ]}, ensure_ascii=False)))
    page.route("**/api/upload", lambda route: pending.append(route))
    page.goto(base_url, wait_until="domcontentloaded")
    page.locator("#entry-paste").click()
    page.wait_for_function("!document.querySelector('#btn-upload').disabled")
    assert page.locator("#category option").count() == 1
    assert page.locator("#category").input_value() == "lease"
    assert page.locator("#category option").inner_text() == "新租赁名称"
    page.locator("#paste-text").fill("甲" * 200001)
    assert page.locator("#btn-upload").is_disabled()
    page.locator("#paste-text").fill("测试合同")
    page.locator("#btn-upload").click()
    page.wait_for_function("document.querySelector('#btn-upload').disabled")
    page.locator("#entry-file").click()
    assert page.locator("#btn-upload").is_disabled()
    page.locator("#entry-paste").click()
    assert page.locator("#btn-upload").is_disabled()
    assert len(pending) == 1
    pending[0].fulfill(status=200, content_type="application/json", body=json.dumps({
        "status": "category_confirm", "precheck": {"detected_type": "测试", "summary": "已收到"}
    }, ensure_ascii=False))
    page.locator("#precheck-dialog").wait_for(state="visible")


def test_real_browser_uses_database_rule_snapshot(pw_browser, tmp_path):  # noqa: F811
    import hashlib
    import uuid

    import yaml

    from app.services.account_store import AccountStore
    from app.services.rule_snapshot import capture_rule_snapshot
    from app.services.rule_versions import RuleVersionStore

    account_db = tmp_path / "accounts.sqlite"
    accounts = AccountStore(account_db)
    admin = accounts.bootstrap_admin("admin", "测试管理员", "testing-password-123")
    token = accounts.login("admin", "testing-password-123")
    base = capture_rule_snapshot("procurement")
    RuleVersionStore(accounts).initialize(token, "procurement", base.config_bytes)
    config = base.configuration()
    next(item for item in config["items"] if item["id"] == "subject_matter")["enabled"] = False
    content = yaml.safe_dump(config, allow_unicode=True).encode()
    digest = hashlib.sha256(content).hexdigest()
    version_id = uuid.uuid4().hex
    with accounts._connect() as conn:
        conn.execute("INSERT INTO rule_versions VALUES (?,?,?,?,?,?)", (version_id, "procurement", digest, content, admin["id"], time.time()))
        conn.execute("UPDATE rule_active_versions SET version_id=?, generation=2 WHERE category='procurement'", (version_id,))
    port = _free_port()
    env = {k: os.environ[k] for k in ("PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP", "APPDATA", "LOCALAPPDATA", "USERPROFILE") if k in os.environ}
    env.update(ACCOUNT_LOCAL_ENABLED="1", ACCOUNT_DB_PATH=str(account_db), STORE_DB_PATH=str(tmp_path / "reviews.sqlite"),
               RULE_EXECUTION_DB_TRIAL="1", PRECHECK_ENABLED="false", CATEGORY_SUGGEST_ENABLED="false", PYTHONIOENCODING="utf-8")
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    context = None
    url = f"http://127.0.0.1:{port}"
    try:
        context = pw_browser.new_context(viewport={"width": 1280, "height": 900})
        deadline = time.time() + 45
        while True:
            try:
                with urllib.request.urlopen(url + "/health", timeout=1):
                    break
            except OSError:
                if proc.poll() is not None:
                    pytest.fail(f"local trial server exited with code {proc.returncode}")
                if time.time() >= deadline:
                    pytest.fail("local trial server did not start")
                time.sleep(0.2)
        page = context.new_page()
        page.goto(url, wait_until="domcontentloaded")
        page.locator("#rule-execution-trial-warning").wait_for(state="visible")
        page.locator("#entry-paste").click()
        page.locator("#paste-text").fill("采购合同。甲方向乙方采购测试设备。")
        page.locator("#btn-upload").click()
        page.locator("#results-body").wait_for(state="visible", timeout=30000)
        assert "数据库试验版本" in page.locator("#results-meta").inner_text()
        assert digest[:12] in page.locator("#results-meta").inner_text()
        href = page.locator("#btn-export-report").get_attribute("href")
        review_id = href.rstrip("/").split("/")[-2]
        review = page.evaluate("async id => (await (await fetch('/api/review/' + id)).json())", review_id)
        assert review["rule_provenance"]["version_id"] == version_id
        assert review["rule_provenance"]["generation"] == 2
        assert review["rule_provenance"]["content_sha256"] == digest
        assert "subject_matter" not in {item["id"] for item in review["items"]}
    finally:
        try:
            if context is not None:
                context.close()
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)


def test_candidate_review_uses_current_account_and_shows_scope(desktop_page, base_url):  # noqa: F811
    page = desktop_page
    calls = []
    version = {"category": "procurement", "label": "采购合同", "generation": 1, "sha256": "a" * 64,
               "items": [{"id": "subject", "name": "主体", "description": "说明", "enabled": True, "has_hardline": False}],
               "segments": [{"key": "A", "name": "主体"}]}

    def respond(route):
        request = route.request
        path = request.url.split("/api/local-accounts", 1)[1].split("?", 1)[0]
        calls.append((request.method, path, request.headers.get("x-csrf-token")))
        if path == "/capability":
            body = {"available": True, "protects_contracts": False}
        elif path == "/session":
            body = {"csrf_token": "token", "user": {"id": "admin", "display_name": "管理员", "role": "admin", "must_change_password": False}}
        elif path == "/rules/versions":
            body = {"versions": [version], "effective_in_reviews": False, "validation_blocked": False}
        elif path == "/rules/candidates":
            body = {"candidates": []}
        else:
            body = {"users": []}
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body, ensure_ascii=False))

    page.route("**/api/local-accounts/**", respond)
    page.goto(base_url, wait_until="domcontentloaded")
    page.locator("#btn-local-accounts").click()
    page.get_by_text("管理员 · 管理员").wait_for()
    page.locator("#account-close").click()
    page.locator("#btn-rule-review").click()
    page.get_by_text("候选和审核结果不会改变现有审查规则").wait_for()
    page.locator("#rule-review-version").select_option("procurement")
    field = page.locator("#rule-review-item-id").bounding_box()
    close = page.locator("#rule-review-close").bounding_box()
    assert field and field["width"] >= 200 and field["height"] >= 36
    assert close and close["height"] >= 32
    assert ("GET", "/rules/versions", "token") in calls


def test_global_validation_block_disables_candidate_controls(desktop_page, base_url):  # noqa: F811
    page = desktop_page
    version = {"category": "procurement", "label": "采购合同", "generation": 1, "sha256": "a" * 64,
               "items": [], "segments": []}

    def respond(route):
        path = route.request.url.split("/api/local-accounts", 1)[1].split("?", 1)[0]
        if path == "/capability":
            body = {"available": True}
        elif path == "/session":
            body = {"csrf_token": "token", "user": {"id": "admin", "display_name": "管理员", "role": "admin", "must_change_password": False}}
        elif path == "/rules/versions":
            body = {"versions": [version], "validation_blocked": True}
        elif path == "/rules/candidates":
            body = {"candidates": [{"id": "a", "category": "procurement", "status": "待测试", "sha256": "a" * 64}]}
        elif path == "/rules/candidates/a":
            body = {"id": "a", "sha256": "a" * 64, "status": "待测试", "submitted_by": "owner",
                    "reason": "测试", "change": {"before": None, "after": {"name": "测试"}},
                    "examples": {}, "review": None}
        elif path.endswith("/validations"):
            body = {"validations": []}
        else:
            body = {"users": []}
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body, ensure_ascii=False))

    page.route("**/api/local-accounts/**", respond)
    page.goto(base_url, wait_until="domcontentloaded")
    page.locator("#btn-local-accounts").click()
    page.get_by_text("管理员 · 管理员").wait_for()
    page.locator("#account-close").click()
    page.locator("#btn-rule-review").click()
    page.get_by_text("规则测试已暂停", exact=False).wait_for()
    page.locator("#rule-review-list button").click()
    page.locator("#rule-review-detail").wait_for()
    assert page.locator("#rule-review-validate").is_disabled()
    assert page.locator("#rule-review-decision").is_visible()
    assert page.locator('#rule-review-decision button[value="approve"]').is_disabled()
    assert page.locator('#rule-review-decision button[value="reject"]').is_enabled()


def test_timeout_updates_global_pause_without_reopening_dialog(desktop_page, base_url):  # noqa: F811
    page = desktop_page
    state = {"blocked": False}

    def respond(route):
        path = route.request.url.split("/api/local-accounts", 1)[1].split("?", 1)[0]
        if path == "/capability":
            body = {"available": True}
        elif path == "/session":
            body = {"csrf_token": "token", "user": {"id": "admin", "display_name": "管理员", "role": "admin", "must_change_password": False}}
        elif path == "/rules/versions":
            body = {"versions": [], "validation_blocked": state["blocked"]}
        elif path == "/rules/candidates":
            body = {"candidates": [{"id": "a", "category": "procurement", "status": "待测试", "sha256": "a" * 64}]}
        elif path == "/rules/candidates/a":
            body = {"id": "a", "sha256": "a" * 64, "status": "待测试", "submitted_by": "owner",
                    "reason": "测试", "change": {"before": None, "after": {"name": "测试"}},
                    "examples": {}, "review": None}
        elif path == "/rules/candidates/a/validate":
            state["blocked"] = True
            body = {"passed": False, "exit_code": None}
        elif path.endswith("/validations"):
            body = {"validations": []}
        else:
            body = {"users": []}
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body, ensure_ascii=False))

    page.route("**/api/local-accounts/**", respond)
    page.goto(base_url, wait_until="domcontentloaded")
    page.locator("#btn-local-accounts").click()
    page.get_by_text("管理员 · 管理员").wait_for()
    page.locator("#account-close").click()
    page.locator("#btn-rule-review").click()
    page.locator("#rule-review-list button").click()
    page.locator("#rule-review-validate").click()
    page.get_by_text("规则测试已暂停", exact=False).wait_for()
    assert page.locator("#rule-review-validate").is_disabled()
    assert page.locator('#rule-review-decision button[value="approve"]').is_disabled()
    assert page.locator('#rule-review-decision button[value="reject"]').is_enabled()


def test_status_refresh_failure_disables_already_visible_approval(desktop_page, base_url):  # noqa: F811
    page = desktop_page
    state = {"failed": False}

    def respond(route):
        path = route.request.url.split("/api/local-accounts", 1)[1].split("?", 1)[0]
        if path == "/capability":
            body = {"available": True}
        elif path == "/session":
            body = {"csrf_token": "token", "user": {"id": "admin", "display_name": "管理员", "role": "admin", "must_change_password": False}}
        elif path == "/rules/versions":
            if state["failed"]:
                route.fulfill(status=200, content_type="application/json", body="invalid-json")
                return
            body = {"versions": [], "validation_blocked": False}
        elif path == "/rules/candidates":
            body = {"candidates": [{"id": "a", "category": "procurement", "status": "待审核", "sha256": "a" * 64}]}
        elif path == "/rules/candidates/a":
            body = {"id": "a", "sha256": "a" * 64, "status": "待审核", "submitted_by": "owner",
                    "reason": "测试", "change": {"before": None, "after": {"name": "测试"}},
                    "examples": {}, "review": None}
        elif path == "/rules/candidates/a/validate":
            state["failed"] = True
            body = {"passed": False, "exit_code": None}
        elif path.endswith("/validations"):
            body = {"validations": [{"id": "v1", "passed": True, "exit_code": 0,
                                      "passed_tests": 1, "collected_tests": 1, "skipped_tests": 0,
                                      "failed_test_ids": [], "scope": "测试"}]}
        else:
            body = {"users": []}
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body, ensure_ascii=False))

    page.route("**/api/local-accounts/**", respond)
    page.goto(base_url, wait_until="domcontentloaded")
    page.locator("#btn-local-accounts").click()
    page.get_by_text("管理员 · 管理员").wait_for()
    page.locator("#account-close").click()
    page.locator("#btn-rule-review").click()
    page.locator("#rule-review-list button").click()
    approve = page.locator('#rule-review-decision button[value="approve"]')
    assert approve.is_enabled()
    page.locator("#rule-review-validate").click()
    page.get_by_text("规则测试已暂停", exact=False).wait_for()
    assert approve.is_disabled()
    assert page.locator('#rule-review-decision button[value="reject"]').is_enabled()


def test_candidate_controls_wait_for_validation_state(desktop_page, base_url):  # noqa: F811
    page = desktop_page
    delayed = []

    def respond(route):
        path = route.request.url.split("/api/local-accounts", 1)[1].split("?", 1)[0]
        if path == "/rules/versions":
            delayed.append(route)
            return
        if path == "/capability":
            body = {"available": True}
        elif path == "/session":
            body = {"csrf_token": "token", "user": {"id": "admin", "display_name": "管理员", "role": "admin", "must_change_password": False}}
        elif path == "/rules/candidates":
            body = {"candidates": [{"id": "a", "category": "procurement", "status": "待审核", "sha256": "a" * 64}]}
        elif path == "/rules/candidates/a":
            body = {"id": "a", "sha256": "a" * 64, "status": "待审核", "submitted_by": "owner",
                    "reason": "测试", "change": {"before": None, "after": {"name": "测试"}},
                    "examples": {}, "review": None}
        elif path.endswith("/validations"):
            body = {"validations": [{"id": "v1", "passed": True, "exit_code": 0, "passed_tests": 1,
                                      "collected_tests": 1, "skipped_tests": 0, "failed_test_ids": [], "scope": "测试"}]}
        else:
            body = {"users": []}
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body, ensure_ascii=False))

    page.route("**/api/local-accounts/**", respond)
    page.goto(base_url, wait_until="domcontentloaded")
    page.locator("#btn-local-accounts").click()
    page.get_by_text("管理员 · 管理员").wait_for()
    page.locator("#account-close").click()
    page.locator("#btn-rule-review").click()
    page.locator("#rule-review-list button").click()
    page.locator("#rule-review-detail").wait_for()
    assert delayed
    assert page.locator("#rule-review-validate").is_disabled()
    assert page.locator('#rule-review-decision button[value="approve"]').is_disabled()
    assert page.locator('#rule-review-decision button[value="reject"]').is_enabled()
    delayed[0].fulfill(status=200, content_type="application/json", body=json.dumps({"versions": [], "validation_blocked": False}))
    page.wait_for_function("!document.querySelector('#rule-review-validate').disabled")
    assert page.locator('#rule-review-decision button[value="approve"]').is_enabled()
    page.locator("#rule-review-close").click()
    page.locator("#btn-rule-review").click()
    page.locator("#rule-review-list button").click()
    page.wait_for_function("document.querySelector('#rule-review-detail').classList.contains('hidden') === false")
    for _ in range(40):
        if len(delayed) == 2:
            break
        page.wait_for_timeout(50)
    assert len(delayed) == 2
    delayed[1].fulfill(status=200, content_type="application/json", body=json.dumps({"versions": [
        {"category": "procurement", "label": "采购", "generation": 1, "items": [], "segments": []}]}))
    page.wait_for_function("document.querySelector('#rule-review-version').options.length === 2")
    assert page.locator("#rule-review-validate").is_disabled()
    assert page.locator('#rule-review-decision button[value="approve"]').is_disabled()


def test_successful_validation_enables_approval_without_reopening(desktop_page, base_url):  # noqa: F811
    page = desktop_page
    state = {"tested": False}

    def respond(route):
        path = route.request.url.split("/api/local-accounts", 1)[1].split("?", 1)[0]
        if path == "/capability":
            body = {"available": True}
        elif path == "/session":
            body = {"csrf_token": "token", "user": {"id": "admin", "display_name": "管理员", "role": "admin", "must_change_password": False}}
        elif path == "/rules/versions":
            body = {"versions": [], "validation_blocked": False, "validation_state": "ready"}
        elif path == "/rules/candidates":
            body = {"candidates": [{"id": "a", "category": "procurement", "status": "待审核" if state["tested"] else "待测试", "sha256": "a" * 64}]}
        elif path == "/rules/candidates/a":
            body = {"id": "a", "sha256": "a" * 64, "status": "待审核" if state["tested"] else "待测试", "submitted_by": "owner",
                    "reason": "测试", "change": {"before": None, "after": {"name": "测试"}}, "examples": {}, "review": None}
        elif path == "/rules/candidates/a/validate":
            state["tested"] = True
            body = {"passed": True, "exit_code": 0}
        elif path.endswith("/validations"):
            body = {"validations": [{"id": "v1", "passed": True, "exit_code": 0, "passed_tests": 1,
                                      "collected_tests": 1, "skipped_tests": 0, "failed_test_ids": [], "scope": "测试"}] if state["tested"] else []}
        else:
            body = {"users": []}
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body, ensure_ascii=False))

    page.route("**/api/local-accounts/**", respond)
    page.goto(base_url, wait_until="domcontentloaded")
    page.locator("#btn-local-accounts").click()
    page.get_by_text("管理员 · 管理员").wait_for()
    page.locator("#account-close").click()
    page.locator("#btn-rule-review").click()
    page.locator("#rule-review-list button").click()
    page.locator("#rule-review-validate").click()
    page.wait_for_function("!document.querySelector('#rule-review-decision button[value=approve]').disabled")


def test_switching_candidate_and_account_clears_old_sensitive_detail(desktop_page, base_url):  # noqa: F811
    page = desktop_page
    delayed = []
    version = {"category": "procurement", "label": "采购合同", "generation": 1, "sha256": "a" * 64,
               "items": [], "segments": []}

    def respond(route):
        request = route.request
        path = request.url.split("/api/local-accounts", 1)[1].split("?", 1)[0]
        if path == "/capability":
            body = {"available": True, "protects_contracts": False}
        elif path == "/session":
            body = {"csrf_token": "token", "user": {"id": "admin", "display_name": "管理员", "role": "admin", "must_change_password": False}}
        elif path == "/rules/versions":
            body = {"versions": [version], "validation_blocked": False}
        elif path == "/rules/candidates":
            body = {"candidates": [{"id": "a", "category": "procurement", "status": "待审核", "sha256": "a" * 64},
                                   {"id": "b", "category": "procurement", "status": "待审核", "sha256": "b" * 64}]}
        elif path == "/rules/candidates/b":
            delayed.append(route)
            return
        elif path.endswith("/validations"):
            body = {"validations": [{"id": "old", "passed": False, "exit_code": 1,
                                     "passed_tests": 2, "collected_tests": 3, "skipped_tests": 0,
                                     "failed_test_ids": ["failed_case"], "scope": "金标专项"}]}
        elif path == "/rules/candidates/a":
            body = {"id": "a", "sha256": "a" * 64, "status": "待审核", "submitted_by": "owner",
                    "reason": "甲原因", "change": {"before": None, "after": {"name": "甲"}},
                    "examples": {"positive": "SECRET A", "negative": "反例"}, "review": None}
        else:
            body = {"users": []}
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body, ensure_ascii=False))

    page.route("**/api/local-accounts/**", respond)
    page.goto(base_url, wait_until="domcontentloaded")
    page.locator("#btn-local-accounts").click()
    page.get_by_text("管理员 · 管理员").wait_for()
    page.locator("#account-close").click()
    page.locator("#btn-rule-review").click()
    rows = page.locator("#rule-review-list .rule-review-row")
    rows.nth(0).get_by_role("button", name="查看").click()
    page.get_by_text("应命中示例：SECRET A").wait_for()
    assert page.locator('#rule-review-decision button[value="approve"]').is_disabled()
    rows.nth(1).get_by_role("button", name="查看").click()
    page.wait_for_timeout(200)
    assert delayed and page.locator("#rule-review-detail").is_hidden()
    assert "SECRET A" not in page.locator("#rule-review-detail-body").inner_text()
    page.evaluate("""() => {
      window.AgentLocalAccounts.user = () => ({id: 'other', role: 'admin', must_change_password: false});
      window.dispatchEvent(new CustomEvent('agent-local-account'));
    }""")
    assert not page.locator("#rule-review-dialog").evaluate("node => node.open")
    assert "SECRET A" not in page.locator("#rule-review-dialog").inner_text()
    delayed[0].fulfill(status=200, content_type="application/json", body="{}")


def test_admin_rollback_warns_when_refresh_fails(desktop_page, base_url):  # noqa: F811
    page = desktop_page
    calls = []
    completed = []
    old_id, current_id = "a" * 32, "b" * 32

    def respond(route):
        request = route.request
        path = request.url.split("/api/local-accounts", 1)[1].split("?", 1)[0]
        calls.append((path, request.headers.get("x-csrf-token"), request.post_data))
        if path == "/capability":
            body = {"available": True}
        elif path == "/session":
            body = {"csrf_token": "token", "user": {"id": "admin", "display_name": "管理员", "role": "admin", "must_change_password": False}}
        elif path == "/rules/versions":
            body = {"versions": [], "release_trial_available": True, "validation_blocked": False}
        elif path == "/rules/candidates":
            body = {"candidates": []}
        elif path == "/rules/release-history":
            if completed:
                route.fulfill(status=200, content_type="application/json", body="invalid-json")
                return
            body = {"active": [{"category": "procurement", "version_id": current_id, "generation": 2}],
                    "events": [], "events_truncated": True,
                    "versions": [{"id": old_id, "category": "procurement", "sha256": "c" * 64}]}
        elif path == "/rules/rollback":
            completed.append(True)
            body = {"generation": 3}
        else:
            body = {"users": []}
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body, ensure_ascii=False))

    page.route("**/api/local-accounts/**", respond)
    page.on("dialog", lambda dialog: dialog.accept())
    page.goto(base_url, wait_until="domcontentloaded")
    page.locator("#btn-local-accounts").click()
    page.get_by_text("管理员 · 管理员").wait_for()
    page.locator("#account-close").click()
    page.locator("#btn-rule-review").click()
    page.get_by_text("仅显示最近 100 条记录或版本", exact=False).wait_for()
    page.locator("#rule-rollback-target").select_option(old_id)
    page.locator("#rule-rollback-reason").fill("恢复上一版本")
    page.locator("#rule-rollback-submit").click()
    page.get_by_text("回滚已完成，但记录刷新失败；请重新打开规则管理查看。").wait_for()
    assert page.locator("#rule-rollback-submit").is_disabled()
    rollback = next(call for call in calls if call[0] == "/rules/rollback")
    assert rollback[1] == "token"
    assert json.loads(rollback[2])["expected_generation"] == 2


def test_real_local_candidate_gold_and_named_self_review(pw_browser, tmp_path):  # noqa: F811
    from app.services.account_store import AccountStore

    account_db = tmp_path / "accounts.sqlite"
    AccountStore(account_db).bootstrap_admin("admin", "管理员甲", "testing-password-123")
    port = _free_port()
    env = {k: v for k, v in os.environ.items() if k not in ("DEEPSEEK_API_KEY", "ZHIPU_API_KEY", "GLM_API_KEY", "XAI_API_KEY", "GROK_API_KEY")}
    env.update(ACCOUNT_LOCAL_ENABLED="1", ACCOUNT_DB_PATH=str(account_db), STORE_DB_PATH=str(tmp_path / "reviews.sqlite"), PYTHONIOENCODING="utf-8")
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    context = pw_browser.new_context(viewport={"width": 1280, "height": 900})
    url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.time() + 45
        while True:
            try:
                with urllib.request.urlopen(url + "/health", timeout=1):
                    break
            except OSError:
                if time.time() >= deadline:
                    pytest.fail("local rule review server did not start")
                time.sleep(0.2)
        page = context.new_page()
        page.goto(url, wait_until="domcontentloaded")
        page.locator("#btn-local-accounts").click()
        page.locator("#account-login-username").fill("admin")
        page.locator("#account-login-password").fill("testing-password-123")
        page.locator("#account-login-form button[type=submit]").click()
        page.get_by_text("管理员甲 · 管理员").wait_for()
        page.locator("#account-close").click()
        page.locator("#btn-rule-review").click()
        page.locator("#rule-review-init-category").select_option("procurement")
        page.locator("#rule-review-init button[type=submit]").click()
        page.get_by_text("试用基线已初始化，尚未发布").wait_for()
        page.locator("#rule-review-version").select_option("procurement")
        page.locator("#rule-review-item-id").fill("custom_delivery")
        page.locator("#rule-review-name").fill("交付清单")
        page.locator("#rule-review-description").fill("检查交付清单")
        page.locator("#rule-review-segment").select_option("B")
        page.locator("#rule-review-literal").fill("交付清单")
        page.locator("#rule-review-positive").fill("按照交付清单验收")
        page.locator("#rule-review-negative").fill("签收验收单")
        page.locator("#rule-review-reason").fill("明确交付范围")
        page.locator("#rule-review-ack").check()
        page.locator("#rule-review-submit").click()
        page.get_by_text("候选已提交，尚未发布").wait_for()
        page.locator("#rule-review-validate").click()
        page.get_by_text("品类金标通过；仍需人工审核和完整回归").wait_for(timeout=180000)
        page.locator("#rule-review-reason-decision").fill("核对了测试结果")
        page.locator('#rule-review-decision button[value="approve"]').click()
        page.get_by_text("人工审核已记录，尚未发布").wait_for()
        page.get_by_text("管理员自审 · 核对了测试结果", exact=False).wait_for()
    finally:
        context.close()
        proc.terminate()
        proc.wait(timeout=10)
