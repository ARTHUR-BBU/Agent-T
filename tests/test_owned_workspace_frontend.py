"""Browser checks for the opt-in account workspace with synthetic contracts."""
import json
import os
import subprocess
import sys
import time
import urllib.request

import pytest

pytest.importorskip("playwright")
from tests.test_frontend_e2e import base_url, desktop_page, pw_browser, _free_port  # noqa: E402,F401


def test_confirmation_uses_session_bound_form_and_safe_text(desktop_page, base_url):  # noqa: F811
    page = desktop_page
    uploads = []
    category_calls = []
    user = {"id": "alice", "display_name": "甲", "role": "user", "must_change_password": False}

    def accounts(route):
        path = route.request.url.split("/api/local-accounts", 1)[1]
        if path == "/session":
            body, status = {}, 200
        elif path == "/login":
            body, status = {"user": user, "csrf_token": "test-token"}, 200
        elif path == "/contracts/upload":
            uploads.append((route.request.headers.get("x-csrf-token"), route.request.post_data or ""))
            if len(uploads) == 1:
                body = {"status": "category_confirm", "suggested_category": "lease", "summary": "<img src=x onerror=alert(1)>", "confirmation_token": "ticket"}
            else:
                body = {"status": "uploaded", "review_id": "one"}
            status = 200
        elif path == "/contracts/one":
            body, status = {"filename": "<b>合同</b>", "status": "done", "precheck": {"performed": False, "skip_reason": "disabled"}, "items": [{"name": "<img src=x>", "status": "pass", "note": "测试"}]}, 200
        else:
            body, status = {"reviews": []}, 200
        route.fulfill(status=status, content_type="application/json", body=json.dumps(body, ensure_ascii=False))

    page.route("**/api/local-accounts/**", accounts)
    def categories(route):
        category_calls.append(1)
        values = [
            {"id": "procurement", "label": "采购合同", "stances": {"allowed": ["neutral"], "labels": {"neutral": "中性（未声明）"}}},
            {"id": "lease", "label": "租赁合同", "stances": {"allowed": ["neutral"], "labels": {"neutral": "中性（未声明）"}}},
        ] if len(category_calls) == 1 else []
        route.fulfill(status=200, content_type="application/json", body=json.dumps({"categories": values}))

    page.route("**/api/categories", categories)
    page.goto(base_url + "/static/owned_workspace.html")
    page.locator("#username").fill("alice")
    page.locator("#password").fill("test-password")
    page.locator("#login button").click()
    page.locator("#category option").first.wait_for(state="attached")
    page.locator("#text").fill("虚构合同")
    page.locator("#submit").click()
    page.locator("#confirm").wait_for(state="visible")
    assert page.locator("#confirm-summary img").count() == 0
    page.locator("#confirm-switch").click()
    page.locator("#detail-status").get_by_text("审查已完成").wait_for()
    assert page.locator("#detail-title").inner_text() == "<b>合同</b>"
    assert page.locator("#items img").count() == 0
    assert "未执行预审" in page.locator("#detail-precheck").inner_text()
    assert [item[0] for item in uploads] == ["test-token", "test-token"]
    assert 'name="confirmation_token"' in uploads[1][1] and 'name="stance"' not in uploads[1][1]
    page.locator("#logout").click()
    page.locator("#login").wait_for(state="visible")
    page.locator("#password").fill("test-password")
    page.locator("#login button").click()
    page.get_by_text("没有可用的合同类型，请检查规则基线。").wait_for()
    assert page.locator("#category option").count() == 0
    assert page.locator("#submit").is_disabled()


def test_real_account_mode_upload_from_browser(pw_browser, tmp_path):  # noqa: F811
    from app.services.account_store import AccountStore
    from app.services.owned_review_store import OwnedReviewStore
    from app.services.rule_snapshot import capture_rule_snapshot
    from app.services.rule_versions import RuleVersionStore
    from app.services.store import ReviewStore
    from tests.helpers import create_active_account

    path = tmp_path / "accounts.sqlite"
    accounts = AccountStore(path)
    accounts.bootstrap_admin("admin", "管理员", "testing-password-123")
    admin = accounts.login("admin", "testing-password-123")
    create_active_account(accounts, admin, "alice", "甲", "alice-password-123", "user")
    OwnedReviewStore(accounts, ReviewStore(str(path)))
    RuleVersionStore(accounts).initialize(admin, "procurement", capture_rule_snapshot("procurement").config_bytes)
    port = _free_port()
    env = {k: v for k, v in os.environ.items() if k not in ("DEEPSEEK_API_KEY", "ZHIPU_API_KEY", "GLM_API_KEY", "XAI_API_KEY", "GROK_API_KEY")}
    env.update(OWNED_LLM_EXECUTION="1", ACCOUNT_LOCAL_ENABLED="1", ACCOUNT_DB_PATH=str(path), STORE_DB_PATH=str(tmp_path / "shared.sqlite"),
               RULE_EXECUTION_DB_TRIAL="1", CONTRACT_ACCOUNT_READ_TRIAL="1", CONTRACT_ACCOUNT_UPLOAD_TRIAL="1", PRECHECK_ENABLED="false",
               QUALITY_ENABLED="false", OBJECTIONS_ENABLED="false", RATE_LIMIT_UPLOAD_PER_MINUTE="0", PYTHONIOENCODING="utf-8")
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f"http://127.0.0.1:{port}"
    context = pw_browser.new_context(viewport={"width": 1280, "height": 900})
    try:
        deadline = time.time() + 45
        while True:
            try:
                with urllib.request.urlopen(url + "/health", timeout=1):
                    break
            except OSError:
                if time.time() >= deadline:
                    pytest.fail("server did not start")
                time.sleep(.2)
        page = context.new_page()
        page.goto(url)
        page.locator("#username").fill("alice")
        page.locator("#password").fill("alice-password-123")
        page.locator("#login button").click()
        page.locator("#category option").first.wait_for(state="attached")
        page.locator("#text").fill("虚构采购合同：甲方向乙方购买设备，价款为一万元。")
        page.locator("#submit").click()
        page.locator("#detail-status").get_by_text("审查已完成").wait_for(timeout=30000)
        assert page.locator("#items .owned-item").count() > 0
        assert "未执行预审" in page.locator("#detail-precheck").inner_text()
        page.locator("#logout").click()
        page.locator("#login").wait_for(state="visible")
        assert page.locator("#items .owned-item").count() == 0
        assert page.locator("#text").input_value() == ""
        assert not (tmp_path / "shared.sqlite").exists()
    finally:
        context.close()
        proc.terminate()
        proc.wait(timeout=15)
