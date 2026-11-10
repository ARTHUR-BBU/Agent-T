"""Browser regression for draft baselines and unsaved input."""
from __future__ import annotations

import json

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect  # noqa: E402

from tests.test_frontend_e2e import base_url, desktop_page, pw_browser  # noqa: E402,F401


def test_changed_baseline_and_category_switch_preserve_or_clear_input(desktop_page, base_url):  # noqa: F811
    page = desktop_page
    calls = 0

    def respond(route):
        nonlocal calls
        url = route.request.url
        if url.endswith("/capability"):
            body = {"available": True}
        elif url.endswith("/catalog"):
            calls += 1
            body = {"packs": [
                {"id": "technical_service", "label": "技术服务", "base_sha256": ("a" if calls == 1 else "b") * 64,
                 "items": [], "segments": [{"key": "B" if calls == 1 else "C", "name": "分段"}]},
                {"id": "lease", "label": "租赁合同", "base_sha256": "c" * 64,
                 "items": [], "segments": [{"key": "A", "name": "分段"}]},
            ]}
        else:
            body = {"drafts": []}
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body, ensure_ascii=False))

    page.route("**/api/rule-drafts**", respond)
    page.goto(base_url, wait_until="domcontentloaded")
    page.locator("#btn-rule-drafts").click()
    page.locator("#draft-segment").select_option("B")
    page.locator("#draft-name").fill("尚未保存的内容")
    page.locator("#rule-drafts-close").click()
    page.locator("#btn-rule-drafts").click()
    page.get_by_text("规则基线已变化，内容已保留；请核对后清空并新建草稿。").wait_for()
    assert page.locator("#draft-name").input_value() == "尚未保存的内容"
    assert page.locator("#draft-segment").input_value() == ""
    assert page.locator("#draft-save").is_disabled()

    decision = {"accept": False}
    page.on("dialog", lambda dialog: dialog.accept() if decision["accept"] else dialog.dismiss())
    page.locator("#draft-category").select_option("lease")
    assert page.locator("#draft-category").input_value() == "technical_service"
    assert page.locator("#draft-name").input_value() == "尚未保存的内容"
    decision["accept"] = True
    page.locator("#draft-category").select_option("lease")
    assert page.locator("#draft-name").input_value() == ""
    assert page.locator("#draft-segment").input_value() == ""


def test_form_is_disabled_until_catalog_arrives(desktop_page, base_url):  # noqa: F811
    page = desktop_page
    pending = []

    def respond(route):
        if route.request.url.endswith("/catalog"):
            pending.append(route)
        elif route.request.url.endswith("/capability"):
            route.fulfill(status=200, content_type="application/json", body='{"available": true}')
        else:
            route.fulfill(status=200, content_type="application/json", body='{"drafts": []}')

    page.route("**/api/rule-drafts**", respond)
    page.goto(base_url, wait_until="domcontentloaded")
    page.locator("#btn-rule-drafts").click()
    page.wait_for_timeout(200)
    assert pending and page.locator("#draft-name").is_disabled()
    body = {"packs": [{"id": "lease", "label": "租赁", "base_sha256": "a" * 64,
                       "items": [], "segments": [{"key": "A", "name": "分段"}]}]}
    pending[0].fulfill(status=200, content_type="application/json", body=json.dumps(body, ensure_ascii=False))
    expect(page.locator("#draft-name")).to_be_enabled()


def test_pending_save_stays_disabled_after_close_and_reopen(desktop_page, base_url):  # noqa: F811
    page = desktop_page
    pending = []

    def respond(route):
        if route.request.method == "POST":
            pending.append(route)
            return
        if route.request.url.endswith("/catalog"):
            body = {"packs": [{"id": "lease", "label": "租赁", "base_sha256": "a" * 64,
                               "items": [], "segments": [{"key": "A", "name": "分段"}]}]}
        elif route.request.url.endswith("/capability"):
            body = {"available": True}
        else:
            body = {"drafts": [{"id": "a" * 32, "category": "lease", "operation": "edit",
                                "item_id": "one", "name": "旧草稿", "base_sha256": "a" * 64,
                                "revision": 1, "before": None, "after": {"name": "旧草稿"},
                                "validation": {"scope": "尚未审核"}}]}
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body, ensure_ascii=False))

    page.route("**/api/rule-drafts**", respond)
    page.goto(base_url, wait_until="domcontentloaded")
    page.locator("#btn-rule-drafts").click()
    page.locator("#draft-segment").select_option("A")
    page.locator("#draft-item-id").fill("example")
    page.locator("#draft-name").fill("未完成保存")
    page.locator("#draft-description").fill("说明")
    page.locator("#draft-literal").fill("关键词")
    page.locator("#draft-positive").fill("有关键词")
    page.locator("#draft-negative").fill("没有该词")
    page.locator("#draft-reason").fill("理由")
    page.locator("#draft-save").click()
    page.wait_for_timeout(200)
    assert pending and page.locator("#draft-name").is_disabled()
    assert page.get_by_role("button", name="继续编辑").is_disabled()
    page.locator("#rule-drafts-close").click()
    page.locator("#btn-rule-drafts").click()
    page.wait_for_timeout(200)
    assert page.locator("#draft-name").is_disabled()
    assert page.get_by_role("button", name="继续编辑").is_disabled()
    pending[0].fulfill(status=200, content_type="application/json", body="{}")
    page.get_by_text("草稿已保存，尚未生效。").wait_for()
    expect(page.locator("#draft-name")).to_be_enabled()


def test_old_draft_list_response_cannot_replace_newer_list(desktop_page, base_url):  # noqa: F811
    page = desktop_page
    pending_post, pending_list = [], []
    list_calls = 0

    def record(name):
        return {"id": "a" * 32, "category": "lease", "operation": "edit", "item_id": "one",
                "name": name, "base_sha256": "a" * 64, "revision": 1,
                "before": None, "after": {"name": name}, "validation": {"scope": "草稿"}}

    def respond(route):
        nonlocal list_calls
        url = route.request.url
        if route.request.method == "POST":
            pending_post.append(route)
            return
        if url.endswith("/catalog"):
            body = {"packs": [{"id": "lease", "label": "租赁", "base_sha256": "a" * 64,
                               "items": [], "segments": [{"key": "A", "name": "分段"}]}]}
        elif url.endswith("/capability"):
            body = {"available": True}
        else:
            list_calls += 1
            if list_calls == 2:
                pending_list.append(route)
                return
            body = {"drafts": [record("最新草稿")] if list_calls >= 3 else []}
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body, ensure_ascii=False))

    page.route("**/api/rule-drafts**", respond)
    page.goto(base_url, wait_until="domcontentloaded")
    page.locator("#btn-rule-drafts").click()
    page.locator("#draft-segment").select_option("A")
    for key, value in {"item-id": "example", "name": "待保存", "description": "说明", "literal": "关键词",
                       "positive": "有关键词", "negative": "没有该词", "reason": "理由"}.items():
        page.locator("#draft-" + key).fill(value)
    page.locator("#draft-save").click()
    page.wait_for_timeout(200)
    assert pending_post
    page.locator("#rule-drafts-close").click()
    page.locator("#btn-rule-drafts").click()
    page.wait_for_timeout(200)
    assert pending_list
    pending_post[0].fulfill(status=200, content_type="application/json", body="{}")
    page.get_by_text("租赁 · 修改 · 最新草稿（未生效）").wait_for()
    pending_list[0].fulfill(status=200, content_type="application/json",
                            body=json.dumps({"drafts": [record("旧草稿")]}, ensure_ascii=False))
    page.wait_for_timeout(200)
    assert page.get_by_text("租赁 · 修改 · 最新草稿（未生效）").is_visible()
    assert page.get_by_text("租赁 · 修改 · 旧草稿（未生效）").count() == 0


def test_opening_saved_draft_does_not_silently_discard_input(desktop_page, base_url):  # noqa: F811
    page = desktop_page

    def respond(route):
        if route.request.url.endswith("/catalog"):
            body = {"packs": [{"id": "lease", "label": "租赁", "base_sha256": "a" * 64,
                               "items": [{"id": "one", "name": "原检查项", "description": "原说明",
                                          "enabled": True, "has_hardline": False}],
                               "segments": [{"key": "A", "name": "分段"}]}]}
        elif route.request.url.endswith("/capability"):
            body = {"available": True}
        else:
            body = {"drafts": [{"id": "a" * 32, "category": "lease", "operation": "edit",
                                "item_id": "one", "name": "保存的草稿", "description": "保存的说明",
                                "base_sha256": "a" * 64, "revision": 1, "before": None,
                                "after": {"name": "保存的草稿"}, "validation": {"scope": "草稿"}}]}
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body, ensure_ascii=False))

    page.route("**/api/rule-drafts**", respond)
    page.goto(base_url, wait_until="domcontentloaded")
    page.locator("#btn-rule-drafts").click()
    page.locator("#draft-name").fill("未保存的内容")
    decision = {"accept": False}
    page.on("dialog", lambda dialog: dialog.accept() if decision["accept"] else dialog.dismiss())
    page.get_by_role("button", name="继续编辑").click()
    assert page.locator("#draft-name").input_value() == "未保存的内容"
    decision["accept"] = True
    page.get_by_role("button", name="继续编辑").click()
    assert page.locator("#draft-name").input_value() == "保存的草稿"
