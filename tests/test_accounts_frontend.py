"""Browser path for the explicitly isolated account experiment."""
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


def test_temporary_password_prompt_and_admin_account_list(desktop_page, base_url):  # noqa: F811
    page = desktop_page
    calls = []

    def respond(route):
        request = route.request
        path = request.url.split("/api/local-accounts", 1)[1]
        calls.append((request.method, path, request.headers.get("x-csrf-token")))
        if path == "/capability":
            body, status = {"available": True, "protects_contracts": False}, 200
        elif path == "/session":
            body, status = {"user": None}, 200
        elif path == "/login":
            body, status = {"csrf_token": "token", "user": {"id": "1", "username": "admin", "display_name": "管理员", "role": "admin", "must_change_password": True}}, 200
        elif path == "/password":
            body, status = {"ok": True}, 200
        else:
            body, status = {"users": []}, 200
        route.fulfill(status=status, content_type="application/json", body=json.dumps(body, ensure_ascii=False))

    page.route("**/api/local-accounts/**", respond)
    page.goto(base_url, wait_until="domcontentloaded")
    page.locator("#btn-local-accounts").click()
    page.locator("#account-login-username").fill("admin")
    page.locator("#account-login-password").fill("temporary-password-123")
    page.locator("#account-close").click()
    page.locator("#btn-local-accounts").click()
    assert page.locator("#account-login-password").input_value() == ""
    page.locator("#account-login-password").fill("temporary-password-123")
    page.locator("#account-login-form button[type=submit]").click()
    page.get_by_text("首次登录请先修改临时密码").wait_for()
    assert page.locator("#account-admin").is_hidden()
    page.locator("#account-current-password").fill("temporary-password-123")
    page.locator("#account-new-password").fill("new-personal-password-123")
    page.locator("#account-password-form button[type=submit]").click()
    page.get_by_text("密码已修改，请重新登录").wait_for()
    assert ("POST", "/password", "token") in calls


def test_admin_list_uses_text_and_session_bound_requests(desktop_page, base_url):  # noqa: F811
    page = desktop_page
    requests = []
    user = {"id": "2", "username": "alice", "display_name": "<img src=x onerror=alert(1)>",
            "role": "user", "active": True, "must_change_password": False}

    def respond(route):
        request = route.request
        path = request.url.split("/api/local-accounts", 1)[1]
        requests.append((request.method, path, request.headers.get("x-csrf-token")))
        if path == "/capability":
            body = {"available": True, "protects_contracts": False}
        elif path == "/session":
            body = {"csrf_token": "token", "user": {"id": "1", "username": "admin", "display_name": "管理员", "role": "admin", "must_change_password": False}}
        elif path == "/users":
            body = {"users": [user]}
        else:
            user["active"] = False
            body = {"ok": True}
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body, ensure_ascii=False))

    page.route("**/api/local-accounts/**", respond)
    page.goto(base_url, wait_until="domcontentloaded")
    page.locator("#btn-local-accounts").click()
    page.get_by_text("<img src=x onerror=alert(1)>", exact=True).wait_for()
    assert page.locator("#account-users img").count() == 0
    page.locator("#account-users").get_by_role("button", name="停用").click()
    page.get_by_text("账户状态已更新").wait_for()
    assert ("GET", "/users", "token") in requests
    assert ("POST", "/users/2/active", "token") in requests


def test_real_local_account_creation_and_first_login(pw_browser, tmp_path):  # noqa: F811
    from app.services.account_store import AccountStore

    account_db = tmp_path / "account.sqlite"
    AccountStore(account_db).bootstrap_admin("admin", "管理员", "testing-password-123")
    port = _free_port()
    env = {k: v for k, v in os.environ.items() if k not in ("DEEPSEEK_API_KEY", "ZHIPU_API_KEY", "GLM_API_KEY", "XAI_API_KEY", "GROK_API_KEY")}
    env.update(ACCOUNT_LOCAL_ENABLED="1", ACCOUNT_DB_PATH=str(account_db), STORE_DB_PATH=str(tmp_path / "reviews.sqlite"), PYTHONIOENCODING="utf-8")
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
                    pytest.fail("local account test server did not start")
                time.sleep(0.2)
        page = context.new_page()
        page.goto(url, wait_until="domcontentloaded")
        page.locator("#btn-local-accounts").click()
        page.locator("#account-login-username").fill("admin")
        page.locator("#account-login-password").fill("testing-password-123")
        page.locator("#account-login-form button[type=submit]").click()
        page.locator("#account-create-username").fill("alice")
        page.locator("#account-create-name").fill("用户甲")
        page.locator("#account-create-password").fill("temporary-password-123")
        page.locator("#account-create-form button[type=submit]").click()
        page.get_by_text("账户已创建，请安全地将临时密码交给使用者").wait_for()
        page.locator("#account-logout").click()
        page.locator("#account-login-username").fill("alice")
        page.locator("#account-login-password").fill("temporary-password-123")
        page.locator("#account-login-form button[type=submit]").click()
        page.get_by_text("首次登录请先修改临时密码").wait_for()
        assert page.locator("#account-admin").is_hidden()
        page.locator("#account-current-password").fill("temporary-password-123")
        page.locator("#account-new-password").fill("personal-password-123")
        page.locator("#account-password-form button[type=submit]").click()
        page.get_by_text("密码已修改，请重新登录").wait_for()
        page.locator("#account-login-password").fill("personal-password-123")
        page.locator("#account-login-form button[type=submit]").click()
        page.get_by_text("用户甲 · 普通用户").wait_for()
    finally:
        context.close()
        proc.terminate()
        proc.wait(timeout=10)
