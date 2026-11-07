"""前端 E2E 测试（Playwright + Chromium，无 Key 模式）。

测试面：上传页 → 上传合同 → 结果渲染（清单/评分卡缺省态/报告导出）→
条目详情 → 追问入口（未开通横幅）→ 导航返回。全程无 LLM 调用，行为确定。

依赖：pip install playwright && playwright install chromium
未安装时本模块整体 skip，不影响其余测试套件。
"""
from __future__ import annotations

import os
import json
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
import pytest

pw = pytest.importorskip("playwright", reason="需要 pip install playwright")
from playwright.sync_api import sync_playwright  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "fixtures" / "lease_sample.txt"

# 本模块内所有用例共享一个无 Key 服务进程（审查同步完成，无需隔离状态）
_LLM_KEY_VARS = ("DEEPSEEK_API_KEY", "ZHIPU_API_KEY", "GLM_API_KEY", "XAI_API_KEY", "GROK_API_KEY")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def base_url():
    """拉起无 Key 模式的 uvicorn 子进程（评审行为确定：评分未开通、追问未开通）。
    全量回归高负载下偶发启动抖动：失败自动换端口重试（最多 3 次）。"""
    env = {k: v for k, v in os.environ.items() if k not in _LLM_KEY_VARS}
    env["PYTHONPATH"] = str(ROOT)
    env["PYTHONIOENCODING"] = "utf-8"
    env["BLIND_SPOT_ENABLED"] = "true"
    # 限频关闭（阶段 0.5）：子进程不继承 conftest 的 autouse 关闭——
    # 本模块用例远超默认 10 次 upload/分钟，不关必 429
    env["RATE_LIMIT_UPLOAD_PER_MINUTE"] = "0"
    env["RATE_LIMIT_ASK_PER_MINUTE"] = "0"
    import urllib.request

    url = proc = None
    last_err = ""
    for attempt in range(3):
        port = _free_port()
        url = f"http://127.0.0.1:{port}"
        # stderr 落临时文件：起不来时留诊断信息（小智娘 P2：DEVNULL 会让失败零线索）
        log_path = Path(tempfile.gettempdir()) / f"e2e_uvicorn_{port}.log"
        with log_path.open("w", encoding="utf-8") as log_file:
            proc = subprocess.Popen(
                [sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(port),
                 "--log-level", "warning"],
                cwd=str(ROOT), env=env,
                stdout=subprocess.DEVNULL, stderr=log_file,
            )
            # 轮询 /health 就绪（uvicorn 启动约 1-2s）
            deadline = time.time() + 45
            ready = False
            while time.time() < deadline:
                try:
                    with urllib.request.urlopen(f"{url}/health", timeout=2) as r:
                        if r.status == 200:
                            ready = True
                            break
                except OSError:
                    time.sleep(0.3)
        if ready:
            break
        proc.terminate()
        proc.wait(timeout=5)
        last_err = log_path.read_text(encoding="utf-8", errors="replace")[-500:]
        log_path.unlink(missing_ok=True)
    else:
        pytest.fail(
            f"E2E 服务进程 3 次尝试均未就绪，最后一次 uvicorn 日志尾部：\n{last_err}"
        )
    # Key 混入探针（肉饼 P2）：白名单剔除若静默失效，服务会带真实 Key 跑 LLM，
    # 金标断言与确定性全部作废——这里显式 fail 而不是让用例 flaky
    probe = httpx.post(
        f"{url}/api/upload",
        files={"file": ("probe.txt", "押金不予退还".encode("utf-8"), "text/plain")},
        data={"category": "lease"}, timeout=60,
    )
    try:
        probe_rid = probe.json()["review_id"]
        # 审查为后台任务：轮询到终态再读 scorecard
        reason = None
        for _ in range(100):
            body = httpx.get(f"{url}/api/review/{probe_rid}", timeout=10).json()
            if body.get("status") not in ("processing", "pending"):
                reason = (body.get("scorecard") or {}).get("reason")
                break
            time.sleep(0.2)
        if reason != "no_llm_key":
            proc.terminate()
            pytest.fail(f"E2E 环境混入 LLM Key（scorecard reason={reason!r}），结果不可信")
    except Exception:  # noqa: BLE001
        proc.terminate()
        pytest.fail("Key 探针上传失败，E2E 服务异常")
    yield url
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


@pytest.fixture(scope="module")
def pw_browser():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        yield browser
        browser.close()


def _new_page(pw_browser, viewport=None):
    """独立 context + page；JS 异常/Console error 在 teardown 统一守护
    （小智娘 P2：只挂在单条用例里会让其余用例静默放过 JS 错误）。"""
    ctx = pw_browser.new_context(viewport=viewport)
    pg = ctx.new_page()
    js_errors: list[str] = []
    pg.on("pageerror", lambda e: js_errors.append(str(e)))
    pg.on("console", lambda m: js_errors.append(m.text) if m.type == "error" else None)
    return pg, ctx, js_errors


@pytest.fixture()
def page(pw_browser):
    """平板带视口（900×900）——B7-5b 双栏断点（≥1024）之下=现状守护带：
    既有全部用例语义不变（<1024 DOM/行为逐字节与旧版一致）。桌面新行为
    （双栏/问清楚 dialog）由 desktop_page 专测。"""
    pg, ctx, js_errors = _new_page(pw_browser, viewport={"width": 900, "height": 900})
    yield pg
    ctx.close()
    assert js_errors == [], f"前端 JS 错误：{js_errors}"


@pytest.fixture()
def desktop_page(pw_browser):
    """桌面视口（1280×900，命中 B7-5b ≥1024 双栏断点）：双栏/对话框专用。"""
    pg, ctx, js_errors = _new_page(pw_browser, viewport={"width": 1280, "height": 900})
    yield pg
    ctx.close()
    assert js_errors == [], f"前端 JS 错误：{js_errors}"


@pytest.fixture()
def mobile_page(pw_browser):
    """阶段 2.4 移动视口（375×800，命中 480px 断点）：复用 module 级
    server/browser，JS 错误守护与桌面同规格。"""
    pg, ctx, js_errors = _new_page(pw_browser, viewport={"width": 375, "height": 800})
    yield pg
    ctx.close()
    assert js_errors == [], f"前端 JS 错误：{js_errors}"


def _goto_home(page):
    page.goto(f"{page._e2e_base}")  # type: ignore[attr-defined]
    page.wait_for_load_state("networkidle")


@pytest.fixture()
def home(page, base_url):
    page._e2e_base = base_url  # type: ignore[attr-defined]
    _goto_home(page)
    return page


@pytest.fixture()
def mobile_home(mobile_page, base_url):
    mobile_page._e2e_base = base_url  # type: ignore[attr-defined]
    _goto_home(mobile_page)
    return mobile_page


@pytest.fixture()
def desktop_home(desktop_page, base_url):
    """B7-5b 桌面双栏（≥1024）：双栏布局/问清楚 dialog/↻ 条件化专用。"""
    desktop_page._e2e_base = base_url  # type: ignore[attr-defined]
    _goto_home(desktop_page)
    return desktop_page


def _upload(page, path: Path = FIXTURE, category: str = "lease", stance: str | None = None):
    # B7-6 整壳：设置区需先点入口卡露出（文件/粘贴互斥切换）
    page.click("#entry-file")
    page.click("#entry-file")
    page.set_input_files("#file", str(path))
    page.select_option("#category", category)
    if stance:
        # radio 是视觉隐藏（键盘可达），点它的 label——和真实用户行为一致
        page.click(f"#stance-track label:has(input[value='{stance}'])")
    page.click("#btn-upload")
    # 审查同步完成，等结果体渲染出来
    page.wait_for_selector("#results-body", state="visible", timeout=15000)
    page.wait_for_selector(".item", state="visible", timeout=5000)


def _wait_terminal(page, timeout: int = 30000) -> None:
    """等 done 终态渲染完成（E2E flake 立项施工卡 T1）。

    A5 优化：processing 中间态只要 items 非空即渲染清单（partial）——
    `.item` visible 不代表终态。导出按钮 href 只在 done 渲染路径填充
    （app/static/app.js:533），以它为终态信号：href 就位后评分卡 note/
    完整 meta 等后续渲染也已完成。不接入 _upload（其余用例可能依赖
    partial 期语义），由三个 flake 用例显式调用。"""
    page.wait_for_function(
        "() => { const b = document.querySelector('#btn-export-report');"
        " return b && b.href && b.href.includes('/api/review/'); }",
        timeout=timeout)


# ---------- 上传页 ----------

def test_home_renders_upload_screen(home):
    assert home.title() == "合同审查"
    assert home.is_visible("#ws-empty")
    assert not home.is_visible("#ws-results")
    options = home.eval_on_selector_all(
        "#category option", "els => els.map(e => [e.value, e.textContent])"
    )
    assert [v for v, _ in options] == ["procurement", "nda", "lease"]
    home.click("#entry-file")  # B7-6：设置区由入口卡露出
    assert home.is_visible("#btn-upload")


def test_http_warning_visible_on_http(home):
    home.click("#entry-file")  # B7-6：露出设置区
    """外审 P1-3：HTTP 连接下红线警示常驻（e2e 服务器是 localhost http）。"""
    assert location_protocol(home) == "http:"
    assert home.is_visible("#http-warning")
    assert "请勿上传真实合同" in home.inner_text("#http-warning")


def location_protocol(page):
    return page.evaluate("window.location.protocol")


def test_upload_without_file_shows_error(home):
    home.click("#entry-file")  # B7-6：露出设置区
    home.click("#btn-upload")
    home.wait_for_selector("#upload-error", state="visible")
    assert "请先选择合同文件" in home.inner_text("#upload-error")
    # 仍停留在上传页
    assert home.is_visible("#ws-empty")


def test_stance_segmented_renders_and_resets(home):
    home.click("#entry-file")  # B7-6：露出设置区
    """阶段 1.3①：立场 segmented 默认中性真选中，品类切换重渲+复位。"""
    home.wait_for_selector("#stance-track input[name='stance']", state="attached")
    # 采购：中性/买方 两段，默认中性选中，前馈小字露出
    values = home.eval_on_selector_all(
        "#stance-track input[name='stance']", "els => els.map(e => e.value)"
    )
    assert values == ["neutral", "buyer"]
    assert home.is_checked("input[name='stance'][value='neutral']")
    assert "卖方立场审查暂不支持" in home.inner_text("#stance-note")
    # 切到 NDA：三段（中性/披露方/接收方），复位中性，前馈小字消失
    home.select_option("#category", "nda")
    values = home.eval_on_selector_all(
        "#stance-track input[name='stance']", "els => els.map(e => e.value)"
    )
    assert values == ["neutral", "disclosing", "receiving"]
    assert home.is_checked("input[name='stance'][value='neutral']")
    assert not home.is_visible("#stance-note")
    # 声明行随选中项切换
    home.click("#stance-track label:has(input[value='receiving'])")
    assert "接收方" in home.inner_text("#stance-hint")


def test_stance_submitted_with_upload(home):
    """立场随上传请求上报：结果页声明行与所选一致。"""
    _upload(home, category="lease", stance="lessee")
    assert "承租方" in home.inner_text("#results-meta")


def test_category_switch_resets_stance(home):
    home.click("#entry-file")  # B7-6：露出设置区
    """门禁 P1 回归：confirm 弹窗点「切换品类」后立场必须重渲复位中性——
    程序赋值不触发 change 事件，残留旧立场重传会 422 死路。"""
    import json

    calls = {"n": 0}
    captured = {}

    def handler(route):
        calls["n"] += 1
        if calls["n"] == 1:
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    {
                        "review_id": None,
                        "message": "category_confirm",
                        "status": "category_confirm",
                        "precheck": {
                            "performed": True,
                            "detected_type": "厂房租赁合同",
                            "confidence": "high",
                            "summary": "以租金换取厂房使用权",
                        },
                        "suggested_category": "lease",
                        "supported_categories": [
                            {"id": "procurement", "label": "采购合同"},
                            {"id": "nda", "label": "保密协议（NDA）"},
                            {"id": "lease", "label": "租赁合同"},
                        ],
                    },
                    ensure_ascii=False,
                ),
            )
        else:
            captured["post"] = route.request.post_data or ""
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"review_id": "e2estub000", "message": "uploaded", "status": "uploaded"}),
            )

    def review_route(route):
        # 轮询返回假 done 记录（避免 404/500 触发 JS 错误守护）
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "id": "e2estub000", "filename": "lease_sample.txt",
                    "category": "lease", "category_label": "租赁合同",
                    "status": "done", "stage": "done", "items": [],
                    "scorecard": {"available": False}, "blind_enabled": False,
                    "stance": "neutral", "stance_declaration": "测试声明",
                },
                ensure_ascii=False,
            ),
        )

    home.route("**/api/upload", handler)
    home.route("**/api/review/e2estub000", review_route)
    home.click("#entry-file")
    home.set_input_files("#file", str(FIXTURE))
    home.select_option("#category", "procurement")
    home.click("#stance-track label:has(input[value='buyer'])")
    home.click("#btn-upload")
    home.wait_for_selector("#precheck-dialog[open]")
    # 点「切换」：程序赋值路径必须显式重渲立场（radio 换成租赁枚举 + 复位中性）
    home.click("#btn-precheck-switch")
    values = home.eval_on_selector_all(
        "#stance-track input[name='stance']", "els => els.map(e => e.value)"
    )
    assert values == ["neutral", "lessee"], "切换品类后立场枚举必须重渲"
    home.wait_for_selector("#results-body", state="visible", timeout=15000)
    post = captured["post"]
    assert "lease" in post, "重传品类应为 lease"
    assert "neutral" in post, "重传立场必须已复位中性（残留 buyer 会 422 死路）"
    assert "buyer" not in post


def test_precheck_dialog_element_exists(home):
    """阶段 1.4⑤：确认弹窗为原生 <dialog>（渲染冒烟；confirm 路径无 Key 不触发）。"""
    assert home.evaluate("document.querySelector('#precheck-dialog') instanceof HTMLDialogElement")


def test_legend_shows_four_status_tags(home):
    tags = home.eval_on_selector_all(
        ".legend .tag", "els => els.map(e => e.textContent.trim())"
    )
    assert tags == ["通过", "需关注", "未找到", "不适用"]


# ---------- 完整审查流（租赁金标合同） ----------

def test_full_review_flow_renders_results(home):
    _upload(home)
    _wait_terminal(home)  # review_id 从导出 href 取，必须等终态（flake 家族成员）
    meta = home.inner_text("#results-meta")
    assert "lease_sample.txt" in meta
    assert "租赁合同" in meta
    # 渲染条数与 API 返回一致（render-parity；不硬编码规则数，避免加规则就脆断）
    rows = home.locator("#item-list .item")
    review_id = home.get_attribute("#btn-export-report", "href").rstrip("/").split("/")[-2]
    api_count = home.evaluate(
        f"async () => (await (await fetch('/api/review/{review_id}')).json()).items.length"
    )
    assert rows.count() == api_count
    # 无 Key → 无补盲候选行
    assert home.locator("#item-list .item.blind-candidate").count() == 0
    # lease_sample 金标：signature 需关注；governing_law 老钱裁决后为本类不适用
    assert home.locator("#item-list .item.attention").count() >= 1
    assert home.locator("#item-list .item.pass").count() >= 10
    # 阶段 0.2：终态 stage=done（三段进度的数据源；DOM 不硬断言防 flaky）
    api_stage = home.evaluate(
        f"async () => (await (await fetch('/api/review/{review_id}')).json()).stage"
    )
    assert api_stage == "done"


def test_refresh_preserves_review_via_hash(home):
    """阶段 0.1（阳仔 UI 提案）：reviewId 进 URL hash，刷新不丢审查结果。"""
    _upload(home)
    assert "#/review/" in home.url, "上传成功后 hash 必须携带 review_id"
    home.reload()
    home.wait_for_selector("#results-body", state="visible", timeout=15000)
    home.wait_for_selector(".item", state="visible", timeout=5000)
    assert "#/review/" in home.url
    assert "lease_sample.txt" in home.inner_text("#results-meta")


def test_back_to_upload_clears_hash(home):
    _upload(home)
    assert "#/review/" in home.url
    home.click("#btn-back-upload")
    home.wait_for_selector("#ws-empty", state="visible")
    assert "#/review/" not in home.url, "返回上传页必须清掉旧审查 hash"


def test_attention_item_click_shows_detail_and_ask(home):
    _upload(home)
    home.locator("#item-list .item.attention").first.click()
    home.wait_for_selector("#item-detail", state="visible")
    assert "说明" in home.inner_text("#item-detail")
    # 需关注条目开放「问清楚一点」入口
    assert home.is_visible("#detail-actions")


def test_clause_badge_and_meta_count(home):
    """阶段 1.1：条款索引锚定——meta 行露出段数，详情区露出「所在条款」。"""
    _upload(home)
    assert "已识别条款" in home.inner_text("#results-meta")
    rows = home.locator("#item-list .item")
    found = False
    for i in range(rows.count()):
        rows.nth(i).click()
        badge = home.locator("#detail-clause")
        if badge.is_visible() and "所在条款" in badge.inner_text():
            found = True
            break
    assert found, "至少一个条目应锚定到条款（段落型回退索引也算）"


def test_attention_item_with_quote_gets_keyword_highlight(home):
    """采购金标的需关注项带原文命中：详情区应渲染 <mark> 关键词高亮
    （lease_sample 的需关注项是缺失型、无摘句，故换采购金标验证）。"""
    _upload(home, path=ROOT / "fixtures" / "procurement_sample.txt", category="procurement")
    # 点开一个带摘句的需关注条目
    rows = home.locator("#item-list .item.attention")
    for i in range(rows.count()):
        rows.nth(i).click()
        if home.locator("#detail-quote mark.kw").count() > 0:
            break
    assert home.locator("#detail-quote mark.kw").count() > 0, "命中关键词应高亮为 mark.kw"


def test_pass_item_click_hides_ask_action(home):
    _upload(home)
    home.locator("#item-list .item.pass").first.click()
    home.wait_for_selector("#item-detail", state="visible")
    # 通过条目不提供追问入口（追问只面向需关注）
    assert not home.is_visible("#detail-actions")


def test_item_row_keyboard_activation(home):
    """行是 role=button + tabIndex=0，Enter 应等价点击（可访问性约定）。"""
    _upload(home)
    row = home.locator("#item-list .item.attention").first
    row.focus()
    home.keyboard.press("Enter")
    home.wait_for_selector("#item-detail", state="visible")


def test_selected_row_highlight(home):
    _upload(home)
    first = home.locator("#item-list .item").first
    first.click()
    assert "selected" in (first.get_attribute("class") or "")


# ---------- 评分卡缺省态（无 Key） ----------

def test_scorecard_hidden_with_no_key_note(home):
    _upload(home)
    _wait_terminal(home)  # 评分卡 note 是 done 终态渲染（flake 家族成员）
    assert not home.is_visible("#score-card")
    note = home.locator(".score-unavailable")
    assert note.count() == 1
    assert "评分暂未开通" in note.inner_text()


# ---------- M4 报告导出 ----------

def test_export_report_button_and_download(home):
    _upload(home)
    _wait_terminal(home)  # 导出 href 是 done 终态填充（flake 家族成员）
    # 阶段 2.3 IA 重排：导出按钮并入 meta 卡（#meta-actions 静态子容器）
    assert home.is_visible("#meta-actions")
    href = home.get_attribute("#btn-export-report", "href")
    assert href and href.startswith("/api/review/") and href.endswith("/report")
    with home.expect_download() as dl_info:
        home.click("#btn-export-report")
    download = dl_info.value
    assert download.suggested_filename.endswith(".docx")
    # 独立临时目录：并发安全，失败也不残留（肉饼 P3）
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "report.docx"
        download.save_as(str(out))
        data = out.read_bytes()
        assert len(data) > 1000, "导出的 docx 不应为空"
        assert data[:2] == b"PK", "docx 应为 zip 容器（PK 魔数），防止 HTML 错误页假通过"


# ---------- 追问屏（未开通横幅） ----------

def test_ask_screen_unavailable_banner(home):
    _upload(home)
    home.locator("#item-list .item.attention").first.click()
    home.click("#detail-ask")
    home.wait_for_selector("#ask-workbench .ask-interact", state="visible")
    banner = home.locator("#ask-unavailable")
    assert banner.is_visible()
    assert "追问暂未开通" in banner.inner_text()
    assert home.locator("#btn-ask").is_disabled()
    assert home.locator("#ask-input").is_disabled()


def test_ask_screen_shows_context_and_quote(home):
    _upload(home, path=ROOT / "fixtures" / "procurement_sample.txt", category="procurement")
    home.locator("#item-list .item.attention").first.click()
    note = home.inner_text("#detail-note")
    home.click("#detail-ask")
    home.wait_for_selector("#ask-workbench .ask-interact", state="visible")
    # 上下文与规则说明一致；采购金标需关注项带摘句，追问页原文应同步展示
    assert home.inner_text("#ask-context").strip() == note.strip()
    assert not home.locator("#ask-quote").evaluate("el => el.classList.contains('empty')")


# ---------- 导航 ----------

def test_back_navigation_ask_to_results(home):
    """B7-6：问清楚不再是独立屏——工作台展开后结果区始终在，点条目即切换。"""
    _upload(home)
    home.locator("#item-list .item.attention").first.click()
    home.click("#detail-ask")
    home.wait_for_selector("#ask-workbench .ask-interact", state="visible")
    assert home.is_visible("#ws-results")
    assert home.is_visible("#ask-workbench")
    # 点另一条：工作台还在、详情切到新条目（任何跳转不丢当前位置）
    home.locator("#item-list .item").last.click()
    assert home.is_visible("#ws-results")
    assert home.is_visible("#ask-workbench")


def test_back_navigation_results_to_upload(home):
    _upload(home)
    home.click("#btn-back-upload")
    assert home.is_visible("#ws-empty")
    assert not home.is_visible("#ws-results")


# ---------- 前端健康度 ----------

def test_escape_html_in_quote(home):
    """escapeHtml 是详情区 innerHTML 拼接的唯一防线（XSS）：
    合同文本中的脚本标签必须按纯文本渲染，不得产生真实元素或执行事件。"""
    payload = (
        "出租方（甲方）：某某置业有限公司。承租方（乙方）：某某科技有限公司。\n"
        "租赁期限自2026年10月1日起至2027年9月30日止。月租金1万元，押二付三。\n"
        "押金不予退还<img src=x onerror=\"window.__xss=1\">，双方确认。\n"
        "争议向法院起诉。本合同适用中华人民共和国法律。双方签字并加盖公章。"
    )
    tmp = Path(tempfile.gettempdir()) / "e2e_xss_contract.txt"
    tmp.write_text(payload, encoding="utf-8")
    try:
        _upload(home, path=tmp, category="lease")
        rows = home.locator("#item-list .item.attention")
        for i in range(rows.count()):
            rows.nth(i).click()
            if "<img" in home.inner_text("#detail-quote"):
                break
        assert "<img" in home.inner_text("#detail-quote"), "payload 应以纯文本出现在摘句中"
        assert home.locator("#detail-quote img").count() == 0, "详情区不得渲染出 img 元素"
        assert home.evaluate("typeof window.__xss === 'undefined'"), "onerror 不得被执行"
    finally:
        tmp.unlink(missing_ok=True)


def test_full_flow_smoke(home):
    """完整用户路径冒烟；JS 异常/Console error 已由 page fixture teardown 统一守护。"""
    _upload(home)
    home.locator("#item-list .item.attention").first.click()
    home.click("#detail-ask")
    home.wait_for_selector("#ask-workbench .ask-interact", state="visible")
    assert home.is_visible("#ws-results")
    # 左栏历史记录应出现当前审查
    assert home.is_visible("#shell-history")


# ---------- 阶段 2.1 质量层（route stub 注入假 done 记录——无 Key 环境测 UI） ----------

def _stub_ai_findings(quality):
    """按服务端 review_findings 合并口径造 ai_findings 载荷（B7-5a 统一面板）。"""
    obs = ((quality or {}).get("observations") or []) \
        if (quality or {}).get("available") else []
    findings = []
    for i, o in enumerate(obs):
        dim = o.get("dimension") or ""
        findings.append({
            "finding_key": f"obs:{dim}:stub{i:02d}",
            "source_type": "observ",
            "fact_type": dim != "impact",
            "dimension": dim,
            "title": o.get("title") or "",
            "comment": o.get("comment") or "",
            "quote": o.get("quote") or "",
            "clause_id": o.get("clause_id") or "",
            "evidence": None,
            "status": "pending" if dim != "impact" else "viewed",
            "question_id": None,
            "rule_link": "",
            "materialized": False,
            "item_id": None,
            "item_status": None,
        })
    return {"available": True, "findings": findings}


def _quality_review_stub(quality):
    """假 done 记录：1 条需关注 + 指定 quality 载荷 + 条款索引（供锚点查表）。"""
    return {
        "id": "e2eq00001", "filename": "lease_sample.txt",
        "category": "lease", "category_label": "租赁合同",
        "status": "done", "stage": "done",
        "items": [{
            "id": "sublet", "name": "转租限制", "status": "需关注",
            "note": "疑似转租", "quote": "本房屋系出租方转租取得", "hits": ["转租"],
            "clause_ids": ["c04"], "primary_clause_id": "c04",
        }, {
            # 门禁 P1 回归素材：snake_case 清单 id（正则漏下划线时此条路由失败）
            "id": "early_termination", "name": "提前解约", "status": "通过",
            "note": "约定了提前解约的书面通知义务", "quote": "本合同一式两份",
            "hits": [], "clause_ids": [], "primary_clause_id": None,
        }],
        "scorecard": {"available": False, "reason": "no_llm_key"},
        "blind_enabled": False,
        "clause_index": {"strategy": "numbered", "count": 2, "clauses": [
            {"id": "c01", "heading": "第一条 租金", "start": 0, "end": 20, "chars": 20},
            {"id": "c04", "heading": "第四条 房屋使用", "start": 40, "end": 80, "chars": 40},
        ]},
        "stance": "neutral", "stance_declaration": "测试声明", "ask_available": False,
        "quality": quality,
        "ai_findings": _stub_ai_findings(quality),
    }


def _stub_quality_flow(page, quality, *, ask_available=False):
    """拦截 upload + review 轮询，注入带 quality 的假 done 记录并走到结果页。"""
    import json as _json

    stub = _quality_review_stub(quality)
    if ask_available:
        stub["ask_available"] = True
    page.route(
        "**/api/upload",
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({"review_id": "e2eq00001", "message": "uploaded", "status": "uploaded"}),
        ),
    )
    page.route(
        "**/api/review/e2eq00001",
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps(stub, ensure_ascii=False),
        ),
    )
    # 点行会触发三卡只读恢复（revision/state）；stub 单没有草案记录，
    # 空态 200 即可（不挡的话打真服务器 404 会进 js_errors 守卫）
    page.route(
        "**/api/review/e2eq00001/items/*/revision/state",
        lambda route: route.fulfill(
            status=200, content_type="application/json", body="{}"),
    )
    page.click("#entry-file")
    page.click("#entry-file")
    page.set_input_files("#file", str(FIXTURE))
    page.click("#btn-upload")
    page.wait_for_selector("#results-body", state="visible", timeout=15000)


def test_ai_findings_panel_renders_dimensions(home):
    """B7-5a 统一面板：徽章/维度 chip/quote/comment/条款锚齐全，容器为虚线边框。"""
    _stub_quality_flow(home, {
        "available": True, "reason": None,
        "observations": [
            {"dimension": "completeness", "title": "缺交付验收安排",
             "quote": "第四条 本合同一式两份", "clause_id": None,
             "comment": "异议期过短，建议延长。", "needs_confirm": True},
            {"dimension": "consistency", "title": "付款与验收矛盾",
             "quote": "月租金5000元", "clause_id": "c01",
             "comment": "未约定验收标准，建议附清单。", "needs_confirm": True},
            {"dimension": "impact", "title": "押金退还条件空悬",
             "quote": "押金2000元", "clause_id": "c04",
             "comment": "退还触发条件不明，建议补细则。", "needs_confirm": True},
        ],
        "disclaimer": "以上为 AI 观察，仅供参考。", "dropped_count": 0, "coverage": None,
    })
    assert home.is_visible("#ai-findings-panel")
    assert "AI 发现" in home.inner_text("#ai-findings-panel .source-badge.quality")
    items = home.query_selector_all("#ai-findings-panel .quality-item")
    assert len(items) == 3
    dims = [el.inner_text() for el in home.query_selector_all("#ai-findings-panel .quality-dim")]
    # 九哥人话化文案（09-13）：等待页副标题与结果页 chip 逐字呼应
    assert dims == ["有没有漏写", "有没有自相矛盾", "对您的影响"]
    # B7-5a 流水线：事实类待确认；观点类标「观点·无核对」
    assert "待确认" in home.inner_text("#ai-findings-panel")
    assert "观点·无核对" in home.inner_text("#ai-findings-panel")
    # 条款锚：clause_id=c01 → 标题查表
    assert "第一条 租金" in home.inner_text("#ai-findings-panel")
    # 虚线容器规格锁定（路线图 2.1 UI 规格）
    border_style = home.eval_on_selector(
        "#ai-findings-panel", "el => getComputedStyle(el).borderTopStyle"
    )
    assert border_style == "dashed"


def test_quality_panel_hidden_without_quality_key(home):
    """旧记录兼容：done 记录无 quality 键 → 整卡静默隐藏。"""
    stub = _quality_review_stub(None)
    stub.pop("quality")
    import json as _json
    home.route(
        "**/api/upload",
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({"review_id": "e2eq00001", "message": "uploaded", "status": "uploaded"}),
        ),
    )
    home.route(
        "**/api/review/e2eq00001",
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps(stub, ensure_ascii=False),
        ),
    )
    home.click("#entry-file")
    home.set_input_files("#file", str(FIXTURE))
    home.click("#btn-upload")
    home.wait_for_selector("#results-body", state="visible", timeout=15000)
    assert not home.is_visible("#ai-findings-panel")


def test_quality_panel_hidden_when_unavailable(home):
    """降级兼容：available=false（如 llm_error）→ 整卡静默隐藏，不渲染灰字噪音。"""
    _stub_quality_flow(home, {"available": False, "reason": "llm_error",
                              "observations": [], "disclaimer": "", "dropped_count": 0,
                              "coverage": None})
    assert not home.is_visible("#ai-findings-panel")


def test_quality_panel_escapes_model_text(home):
    """XSS 守护：模型态字段（title/quote/comment）一律 textContent，注入不执行。"""
    _stub_quality_flow(home, {
        "available": True, "reason": None,
        "observations": [{
            "dimension": "impact",
            "title": "<img src=x onerror=window.__pwned=1>",
            "quote": "<script>window.__pwned=2</script>",
            "clause_id": None,
            "comment": "<b>假加粗</b>",
            "needs_confirm": True,
        }],
        "disclaimer": "测试免责", "dropped_count": 0, "coverage": None,
    })
    assert home.is_visible("#ai-findings-panel")
    assert home.evaluate("window.__pwned === undefined"), "注入脚本不得执行"
    assert home.query_selector_all("#ai-findings-panel img") == []
    assert home.query_selector_all("#ai-findings-panel script") == []
    # 原样以文本呈现
    assert "<img src=x" in home.inner_text("#ai-findings-panel")
    assert "<b>假加粗</b>" in home.inner_text("#ai-findings-panel")


def test_quality_stage_analyzing_segment(home):
    """等待页第 4 段「AI 观察」存在；无 Key 环境 analyzing 不点亮即 done。"""
    assert home.query_selector("#stage-list .stage[data-stage='analyzing']")
    stages = home.query_selector_all("#stage-list .stage")
    assert len(stages) == 4
    _upload(home)
    # done 后 analyzing 未被点亮过（无 Key 环境质量层 no_llm_key，但仍会
    # 短暂点亮——这里只断言四段清单与结果页正常抵达）
    assert home.is_visible("#results-body")


# ---------- 阶段 2.3 IA 重排 + 2.2 四段式 ----------

def test_scorecard_folded_with_summary_line(home):
    """评分卡沉底折叠：默认收起、摘要行自带「仅供参考」轻量免责、点开见明细。"""
    stub = _quality_review_stub(None)
    stub["scorecard"] = {
        "available": True, "reason": None, "total": 66,
        "tier": {"label": "有实质风险", "hint": "核心条款有硬伤，先改完再谈签的事"},
        "summary": "规则结果汇总评分 66 分。", "segments": [], "caps_applied": [],
        "disclaimer": "模型评分仅供参考，以逐条规则结论为准", "advisory_only": True,
    }
    import json as _json

    home.route(
        "**/api/upload",
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({"review_id": "e2eq00001", "message": "uploaded", "status": "uploaded"}),
        ),
    )
    home.route(
        "**/api/review/e2eq00001",
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps(stub, ensure_ascii=False),
        ),
    )
    home.click("#entry-file")
    home.set_input_files("#file", str(FIXTURE))
    home.click("#btn-upload")
    home.wait_for_selector("#results-body", state="visible", timeout=15000)

    assert home.is_visible("#score-card")
    # 默认收起：分段明细不可见；摘要行自带轻量免责
    assert not home.locator("#score-card").evaluate("el => el.hasAttribute('open')")
    summary_text = home.inner_text("#score-card summary")
    assert "66" in summary_text and "仅供参考，点开看明细" in summary_text
    assert not home.is_visible("#score-breakdown")
    # 点开：明细可见、hint 切换
    home.click("#score-card > summary")
    assert home.locator("#score-card").evaluate("el => el.hasAttribute('open')")
    assert home.is_visible("#score-card .score-fold-body")
    assert "点开收起" in home.inner_text("#score-card summary")


def test_item_rows_show_note_summary(home):
    """清单行两行结构：每条露 note 首句摘要（caption 灰字）；空 note 不渲染。"""
    _upload(home)
    rows = home.query_selector_all("#item-list .item")
    assert rows
    with_caption = [r for r in rows if r.query_selector(".item-caption")]
    assert with_caption, "至少部分行应有摘要（fixture 合同的 note 均非空）"
    for r in with_caption:
        cap = r.query_selector(".item-caption").inner_text().strip()
        assert cap, "摘要不应为空串"
        # 首句截断：不含句末标点（首句即第一句）
        assert not cap.endswith(("。", "！", "？")), "摘要应是首句截断（不含句末标点）"


def _stub_ask_flow(page, answer_payload, raw_text=None):
    """注入 ask 可用的假 done 记录 + /api/ask 假答案（无 Key 环境测四段式
    渲染的唯一路径：UI 门禁看 review.ask_available，网络层拦截 ask 响应）。"""
    import json as _json

    stub = _quality_review_stub(None)
    stub["ask_available"] = True
    page.route(
        "**/api/upload",
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({"review_id": "e2eq00001", "message": "uploaded", "status": "uploaded"}),
        ),
    )
    page.route(
        "**/api/review/e2eq00001",
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps(stub, ensure_ascii=False),
        ),
    )
    page.route(
        "**/api/review/e2eq00001/items/*/revision/state",
        lambda route: route.fulfill(
            status=200, content_type="application/json", body="{}"),
    )

    page.route(
        "**/api/ask",
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({
                "ok": True, "item_id": "sublet", "item_name": "转租限制",
                "answer": answer_payload, "raw_text": raw_text, "error": None,
            }, ensure_ascii=False),
        ),
    )
    # B7-3a 三卡：点开需关注条目即拉 GET state——stub 环境里审查记录是
    # 假 id，必须一并拦截（空态即可，本组用例只测追问渲染不测三卡）
    page.route(
        "**/api/review/e2eq00001/items/*/revision/state",
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({"status": "ok", "item_id": "sublet",
                              "drafts": [], "selected": None, "impact": None,
                              "remaining": {"revision": 4, "impact": 3}}),
        ),
    )
    page.click("#entry-file")
    page.click("#entry-file")
    page.set_input_files("#file", str(FIXTURE))
    page.click("#btn-upload")
    page.wait_for_selector("#results-body", state="visible", timeout=15000)
    page.wait_for_selector(".item.attention", state="visible", timeout=5000)


def test_ask_answer_renders_four_segment_layout(home):
    """四段式答案：原文依据→实际影响→建议改法(+改写稿)→「还想问」chips。"""
    _stub_ask_flow(home, {
        "风险等级": "高",
        "这条在查啥": "单方解除权约定",
        "原文在哪": "第五条 甲方有权随时解除本合同",
        "问题是啥": "甲方可以随时解约，您的工作安排没有保障。",
        "建议怎么改": "约定解除需提前 30 日书面通知。",
        "改写稿": "第五条 甲方解除本合同须提前三十日书面通知乙方。",
        "还想问": "解除的赔偿怎么算？\n提前通知期可以更长吗？",
    })
    home.locator("#item-list .item.attention").first.click()
    home.click("#detail-ask")
    home.wait_for_selector("#ask-workbench .ask-interact", state="visible")
    home.fill("#ask-input", "这条有什么问题？")
    home.click("#btn-ask")
    # B7-6 对话工作台：本轮问答归档成 .ask-msg 气泡（#ask-answer 槽位
    # 渲染完即清空——断言口径跟着搬到气泡里）
    home.wait_for_selector("#ask-thread .ask-msg", state="visible")
    assert "这条有什么问题？" in home.inner_text("#ask-thread .ask-msg-q")

    labels = [
        el.inner_text().strip()
        for el in home.query_selector_all("#ask-thread .ask-msg .answer .detail-label")
    ]
    assert labels[0] == "原文依据"
    assert labels[1] == "实际影响"
    assert labels[2] == "建议改法"
    # 「不伪造风险等级」禁令：风险等级/这条在查啥不得出现在答案里
    text = home.inner_text("#ask-thread .ask-msg .answer")
    assert "高" != text.strip() and "风险等级" not in text and "单方解除权约定" not in text
    # 改写稿块与复制按钮原样保留
    assert home.is_visible("#ask-rewrite-text")
    assert home.is_visible("#btn-copy-rewrite")
    # 「还想问」chips：输入框上方可点回填
    assert home.is_visible("#ask-followups-wrap")
    chips = home.query_selector_all("#ask-followups li")
    assert len(chips) == 2
    chips[0].click()
    assert home.input_value("#ask-input") == "解除的赔偿怎么算？"


def test_ask_answer_raw_fallback_kept(home):
    """结构化解析失败：raw_text <pre> 兜底保留（坏了不许装正常）。"""
    _stub_ask_flow(home, {}, raw_text="模型的原始输出（结构化失败）")
    home.locator("#item-list .item.attention").first.click()
    home.click("#detail-ask")
    home.wait_for_selector("#ask-workbench .ask-interact", state="visible")
    home.fill("#ask-input", "为什么？")
    home.click("#btn-ask")
    home.wait_for_selector("#ask-thread .ask-msg .answer pre", state="visible")
    assert "原始输出" in home.inner_text("#ask-thread .ask-msg .answer pre")


# ---------- 阶段 2.4 移动端（375×800 命中 480px 断点） ----------

def test_mobile_item_tap_opens_detail_route(mobile_home):
    """B7-6 整壳：移动端点条目=行内详情+对话工作台（hash 详情页随三屏
    模型退役——移动/桌面同一套交互，这是老板拍板的有意行为变更）。"""
    _upload(mobile_home)
    # JS 原生 click：Playwright .click() 会自动把目标滚进视野
    mobile_home.evaluate(
        "document.querySelector('#item-list .item.attention').click()"
    )
    # 行内详情填充：详情头 + 档位 tag（双通道）
    mobile_home.wait_for_selector("#item-detail:not(.hidden)", state="attached")
    assert mobile_home.inner_text("#detail-head").strip() != ""
    assert mobile_home.query_selector("#detail-head .tag.attention")
    mobile_home.click("#detail-ask")  # 问清楚入口：填充工作台上下文
    # 阅读顺序：说明卡在原文卡之前
    # 阅读顺序改 DOM 序断言（像素坐标受滚动/搬迁影响，脆）
    assert mobile_home.evaluate(
        "(() => { const w = document.getElementById('ask-workbench');"
        " const kids = Array.from(w.children).map(c => c.id);"
        " return kids.indexOf('ask-note-card') < kids.indexOf('ask-quote-card'); })()")
    assert mobile_home.inner_text("#ask-context").strip() != ""
    assert mobile_home.inner_text("#ask-context").strip() != ""


def test_mobile_blind_item_hides_ask_card(mobile_home):
    """补盲条目：追问卡整卡隐藏（不能误渲染成「追问暂未开通」）。"""
    stub = _quality_review_stub(None)
    stub["blind_enabled"] = True
    stub["blind_candidates"] = [{
        "id": "sig01", "name": "签署与印章", "status": "需关注",
        "note": "规则未标需关注，模型提出候选风险", "quote": "甲方（签字）：____",
        "hits": [], "tag_source": "blind", "needs_confirm": True,
    }]
    import json as _json

    mobile_home.route(
        "**/api/upload",
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({"review_id": "e2eq00001", "message": "uploaded", "status": "uploaded"}),
        ),
    )
    mobile_home.route(
        "**/api/review/e2eq00001",
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps(stub, ensure_ascii=False),
        ),
    )
    mobile_home.click("#entry-file")
    mobile_home.set_input_files("#file", str(FIXTURE))
    mobile_home.click("#btn-upload")
    mobile_home.wait_for_selector("#results-body", state="visible", timeout=15000)
    mobile_home.locator("#item-list .item.blind-candidate").first.click()
    mobile_home.wait_for_selector("#ask-workbench .ask-interact", state="visible")
    # B7-6：hash 路由退役——补盲选中改由详情头断言（key 完整性走 selectItem）
    assert mobile_home.inner_text("#detail-head").strip() != ""
    # 说明/原文卡照常；追问卡隐藏且不出现「追问暂未开通」横幅
    assert mobile_home.is_visible("#ask-note-card")
    assert mobile_home.is_visible("#ask-workbench .ask-interact")
    assert not mobile_home.is_visible("#ask-unavailable")


def test_mobile_followup_chips_meet_touch_target(mobile_home):
    """还想问 chips：480 内触控高度 ≥44px（现状 ≈20px 是最严重不达标项）。"""
    _stub_ask_flow(
        mobile_home,
        {"原文在哪": "第五条", "问题是啥": "影响说明。", "建议怎么改": "改法。",
         "改写稿": "", "还想问": "解除的赔偿怎么算？"},
    )
    mobile_home.locator("#item-list .item.attention").first.click()
    mobile_home.click("#detail-ask")  # 问清楚入口：填充工作台上下文
    mobile_home.wait_for_selector("#ask-workbench .ask-interact", state="visible")
    mobile_home.fill("#ask-input", "这条有什么问题？")
    mobile_home.click("#btn-ask")
    mobile_home.wait_for_selector("#ask-followups li", state="visible")
    box = mobile_home.query_selector("#ask-followups li").bounding_box()
    assert box["height"] >= 44, f"chips 触控高度应 ≥44（实际 {box['height']}）"


def test_desktop_item_click_keeps_inline_panel(home):
    """桌面回归：≥481px 点条目仍走行内面板，不产生 /item/ hash 路由。"""
    _upload(home)
    home.evaluate("window.scrollTo(0, 0)")
    home.locator("#item-list .item.attention").first.click()
    home.wait_for_selector("#item-detail", state="visible")
    assert "/item/" not in home.evaluate("window.location.hash")
    assert home.is_visible("#ask-workbench")


def test_precheck_keep_button_has_secondary_style(home):
    """双端 bug 顺手修：#btn-precheck-keep 此前漏挂 .secondary（浏览器默认
    高度不达标），现在应有 44px 标准按钮样式。"""
    import json as _json

    calls = {"n": 0}

    def handler(route):
        calls["n"] += 1
        if calls["n"] == 1:
            route.fulfill(
                status=200, content_type="application/json",
                body=_json.dumps(
                    {
                        "review_id": None, "message": "category_confirm",
                        "status": "category_confirm",
                        "precheck": {"performed": True, "detected_type": "厂房租赁合同",
                                     "confidence": "high", "summary": "以租金换取厂房使用权"},
                        "suggested_category": "lease", "supported_categories": [],
                    },
                    ensure_ascii=False,
                ),
            )
        else:
            route.fulfill(
                status=200, content_type="application/json",
                body=_json.dumps({"review_id": "e2estub000", "message": "uploaded", "status": "uploaded"}),
            )

    def review_route(route):
        route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({
                "id": "e2estub000", "filename": "lease_sample.txt", "category": "lease",
                "category_label": "租赁合同", "status": "done", "stage": "done", "items": [],
                "scorecard": {"available": False}, "blind_enabled": False,
                "stance": "neutral", "stance_declaration": "",
            }, ensure_ascii=False),
        )

    home.route("**/api/upload", handler)
    home.route("**/api/review/e2estub000", review_route)
    home.click("#entry-file")
    home.set_input_files("#file", str(FIXTURE))
    home.select_option("#category", "procurement")
    home.click("#btn-upload")
    home.wait_for_selector("#precheck-dialog[open]")
    keep = home.locator("#btn-precheck-keep")
    assert "secondary" in (keep.get_attribute("class") or "")
    assert keep.bounding_box()["height"] >= 40


def test_mobile_underscore_id_item_route(mobile_home):
    """门禁 P1 回归：snake_case 条目 id（early_termination 等）与
    blind:governing_law 形态的 key 在路由里必须完整匹配——正则漏下划线
    会让整体匹配失败，用户被踢回上传页。"""
    stub = _quality_review_stub(None)
    stub["blind_enabled"] = True
    stub["blind_candidates"] = [{
        "id": "governing_law", "name": "管辖法律", "status": "需关注",
        "note": "候选说明", "quote": "本合同一式两份", "hits": [],
        "tag_source": "blind", "needs_confirm": True,
    }]
    import json as _json

    mobile_home.route(
        "**/api/upload",
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({"review_id": "e2eq00001", "message": "uploaded", "status": "uploaded"}),
        ),
    )
    mobile_home.route(
        "**/api/review/e2eq00001",
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps(stub, ensure_ascii=False),
        ),
    )
    mobile_home.route(
        "**/api/review/e2eq00001/items/*/revision/state",
        lambda route: route.fulfill(
            status=200, content_type="application/json", body="{}"),
    )
    mobile_home.click("#entry-file")
    mobile_home.set_input_files("#file", str(FIXTURE))
    mobile_home.click("#btn-upload")
    mobile_home.wait_for_selector("#results-body", state="visible", timeout=15000)
    # ① blind:下划线 key（B7-6：hash 路由退役，行内选中；key 完整性仍由
    #    selectItem 的 selKey 口径保证——详情头出现即选中成功）
    mobile_home.evaluate(
        "document.querySelector('#item-list .item.blind-candidate').click()"
    )
    mobile_home.wait_for_selector("#item-detail:not(.hidden)", state="attached")
    assert "管辖法律" in mobile_home.inner_text("#detail-head")
    # ② 再点 snake_case 真实清单 id：切换选中不踢回上传页
    mobile_home.evaluate(
        "document.querySelector('#item-list .item:not(.blind-candidate)').click()"
    )
    mobile_home.wait_for_selector("#item-detail:not(.hidden)", state="attached")
    assert mobile_home.inner_text("#detail-head").strip() != ""
    assert not mobile_home.is_visible("#ws-empty"), "不得被踢回上传页"


# ---------- 阶段 3 异议层（规则异议候选 + 采纳为提案） ----------

def _objections_review_stub(objections):
    """假 done 记录：在 quality stub 基础上注入 objections 载荷。"""
    stub = _quality_review_stub(None)
    stub["id"] = "e2eo00001"
    stub["objections"] = objections
    return stub


def _stub_objections_flow(page, objections):
    """拦截 upload + review 轮询 + adopt，注入带异议的假 done 记录并走到结果页。"""
    import json as _json

    page.route(
        "**/api/upload",
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({"review_id": "e2eo00001", "message": "uploaded", "status": "uploaded"}),
        ),
    )
    page.route(
        "**/api/review/e2eo00001",
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps(_objections_review_stub(objections), ensure_ascii=False),
        ),
    )
    page.click("#entry-file")
    page.click("#entry-file")
    page.set_input_files("#file", str(FIXTURE))
    page.click("#btn-upload")
    page.wait_for_selector("#results-body", state="visible", timeout=15000)


_OK_OBJECTION = {
    "item_id": "sublet", "rule_id": "sublet#r0", "rule_class": "heuristic",
    "direction": "false_positive", "quote": "本房屋系出租方转租取得",
    "counter_evidence": "未发现反证原文",
    "legal_reasoning": "规则词表对「转租」的命中未区分产权转租与使用权转租，疑似误报候选。",
    "stance_check": "与立场无关",
    "proposal": "为「转租」词表增加产权语境区分。",
    "accepted": True, "reject_reason": None,
    "clause_id": "c04", "clause_ambiguous": False,
    "adopted": False, "needs_confirm": True,
}


def test_objections_panel_renders_and_adopt(home):
    """异议面板渲染 + 采纳按钮走 adopt 端点后变为已采纳态。"""
    import json as _json

    adopted_calls = []
    home.route(
        "**/api/review/e2eo00001/objections/adopt",
        lambda route: (
            adopted_calls.append(_json.loads(route.request.post_data)),
            route.fulfill(
                status=200, content_type="application/json",
                body=_json.dumps(
                    {
                        "available": True, "reason": None,
                        "objections": [{**_OK_OBJECTION, "adopted": True}],
                        "rejected_count": 0,
                        "disclaimer": "异议只是候选线索，不改变逐条核查结论；是否成立由人工与规则修订决定。",
                    },
                    ensure_ascii=False,
                ),
            ),
        ),
    )
    _stub_objections_flow(home, {
        "available": True, "reason": None,
        "objections": [dict(_OK_OBJECTION)],
        "rejected_count": 0,
        "disclaimer": "异议只是候选线索，不改变逐条核查结论；是否成立由人工与规则修订决定。",
    })
    assert home.is_visible("#objections-panel")
    assert "规则异议" in home.inner_text("#objections-panel .source-badge.quality")
    assert "疑似误报" in home.inner_text("#objections-panel")
    assert "待人工确认" in home.inner_text("#objections-panel")
    # 采纳：按钮点击 → POST index=0 → 变已采纳禁用态
    home.click("#objections-panel .objection-adopt")
    home.wait_for_function(
        "document.querySelector('#objections-panel .objection-adopt').textContent.includes('已采纳')"
    )
    assert adopted_calls == [{"index": 0}]


def test_objections_panel_hides_rejected_and_unavailable(home):
    """五要件拒收条目不渲染；available=false 整卡静默隐藏（铁律 3 展示语义）。"""
    _stub_objections_flow(home, {
        "available": True, "reason": None,
        "objections": [{**_OK_OBJECTION, "accepted": False, "reject_reason": "要件①条款引用未能在原文核验"}],
        "rejected_count": 1,
        "disclaimer": "x",
    })
    assert not home.is_visible("#objections-panel")


def test_objections_panel_hidden_without_key(home):
    """旧记录兼容：review 载荷无 objections 键 → 面板静默隐藏。"""
    _stub_objections_flow(home, None)
    assert not home.is_visible("#objections-panel")


# ---------- B7-3a 三卡（route stub：POST 三端点 + 动态 state；复批 1-5） ----------

def _rev_batch(is_selected=False):
    return {"draft_id": "d1", "created_at": 1730000000, "status": "ok",
            "is_selected": is_selected,
            "selected_stance": "平衡" if is_selected else None,
            "versions": [
                {"stance": "稳妥", "text": "改为对等罚则。", "note": "最容易谈成"},
                {"stance": "平衡", "text": "折中方案文本。", "note": "各让一步"},
                {"stance": "进取", "text": "删除没收约定。", "note": "强势时用"},
            ],
            "rationale": "三版差异说明。", "assumptions": [],
            "declaration": "起草辅助参考，不构成法律意见。"}


def _rev_state_payload(phase):
    if phase == "drafted":
        return {"status": "ok", "item_id": "sublet",
                "drafts": [_rev_batch()], "selected": None, "impact": None,
                "remaining": {"revision": 4, "impact": 3}}
    if phase == "selected":
        return {"status": "ok", "item_id": "sublet",
                "drafts": [_rev_batch(is_selected=True)],
                "selected": {"draft_id": "d1", "stance": "平衡"},
                "impact": None, "remaining": {"revision": 4, "impact": 3}}
    if phase == "impact":
        return {"status": "ok", "item_id": "sublet",
                "drafts": [_rev_batch(is_selected=True)],
                "selected": {"draft_id": "d1", "stance": "平衡"},
                "impact": {"bound": {"draft_id": "d1", "stance": "平衡"},
                           "state": "ok", "legacy": False, "stale": False,
                           "findings": [{"index": 0, "note": "口径不一致"}],
                           "hidden_findings": 0, "notice": "仅供关注",
                           "excerpts": [{"excerpt": "第十二条 违约责任"}],
                           "covered": True},
                "remaining": {"revision": 4, "impact": 2}}
    if phase == "degraded":
        return {"status": "ok", "item_id": "sublet",
                "drafts": [{"draft_id": "d1", "created_at": 1730000000,
                            "status": "degraded", "is_selected": False,
                            "selected_stance": None}],
                "selected": None, "impact": None,
                "remaining": {"revision": 4, "impact": 3}}
    return {"status": "ok", "item_id": "sublet", "drafts": [],
            "selected": None, "impact": None,
            "remaining": {"revision": 4, "impact": 3}}


def _stub_rev_flow(page, *, second_attention=False):
    """三卡 stub 流：假 done 记录（sublet 需关注+quote）+ revision 四端点
    拦截。phase 字典驱动 state 返回内容；counts 记 POST 次数（双击防重）。
    走到结果页后返回 (phase, counts)。"""
    import json as _json

    phase = {"state": "empty", "generate_result": "ok", "generate_status": 200}
    counts = {"generate": 0, "select": 0, "impact": 0}
    stub = _quality_review_stub(None)
    stub["id"] = "e2er00001"
    if second_attention:
        stub["items"].insert(1, {
            "id": "deposit", "name": "押金退还", "status": "需关注",
            "note": "退还条件不明", "quote": "押金2000元", "hits": ["押金"],
            "clause_ids": [], "primary_clause_id": None,
        })
    page.route(
        "**/api/upload",
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({"review_id": "e2er00001", "message": "uploaded",
                              "status": "uploaded"}),
        ),
    )
    page.route(
        "**/api/review/e2er00001",
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps(stub, ensure_ascii=False),
        ),
    )
    page.route(
        "**/api/review/e2er00001/items/sublet/revision/state",
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps(_rev_state_payload(phase["state"]),
                             ensure_ascii=False),
        ),
    )
    page.route(
        "**/api/review/e2er00001/items/sublet/revision/select",
        lambda route: (counts.__setitem__(
            "select", counts["select"] + 1),
            phase.__setitem__("state", "selected"),
            route.fulfill(
                status=200, content_type="application/json",
                body=_json.dumps({"status": "selected", "draft_id": "d1",
                                  "stance": "平衡"}))),
    )
    page.route(
        "**/api/review/e2er00001/items/sublet/revision/impact",
        lambda route: (counts.__setitem__(
            "impact", counts["impact"] + 1),
            phase.__setitem__("state", "impact"),
            route.fulfill(
                status=200, content_type="application/json",
                body=_json.dumps({"status": "ok", "report": {}}))),
    )

    def _generate(route):
        counts["generate"] += 1
        if phase["generate_status"] != 200:
            route.fulfill(status=phase["generate_status"], body="err")
            return
        if phase["generate_result"] == "degraded":
            phase["state"] = "degraded"
            route.fulfill(
                status=200, content_type="application/json",
                body=_json.dumps({"status": "degraded",
                                  "message": "生成失败"}),
            )
        else:
            phase["state"] = "drafted"
            route.fulfill(
                status=200, content_type="application/json",
                body=_json.dumps({"status": "ok", "draft_id": "d1"}),
            )

    page.route("**/api/review/e2er00001/items/sublet/revision", _generate)
    if second_attention:
        page.route(
            "**/api/review/e2er00001/items/deposit/revision/state",
            lambda route: route.fulfill(
                status=200, content_type="application/json",
                body=_json.dumps(_rev_state_payload("empty"),
                                 ensure_ascii=False),
            ),
        )
    page.click("#entry-file")
    page.click("#entry-file")
    page.set_input_files("#file", str(FIXTURE))
    page.click("#btn-upload")
    page.wait_for_selector("#results-body", state="visible", timeout=15000)
    page.wait_for_selector(".item.attention", state="visible", timeout=5000)
    return phase, counts


def test_rev_cards_full_chain_busy_resets_and_single_post(home):
    """复批 1：生成→选定→检查全链，busy 每步复位（不卡「正在生成」）；
    busy 守卫双击只发一次 POST；复批 3：导出承诺不上。"""
    _, counts = _stub_rev_flow(home)
    home.locator("#item-list .item.attention").first.click()
    home.wait_for_selector("#rev-cards [data-action=generate]", state="visible")
    # 双击（同步两次 click）：busy 守卫下只发一次 POST
    home.evaluate(
        "() => { const b = document.querySelector("
        "\"#rev-cards [data-action=generate]\"); b.click(); b.click(); }"
    )
    home.wait_for_selector("#rev-cards .rev-version", state="visible")
    assert "正在生成" not in home.inner_text("#rev-cards"), "成功后 busy 必须复位"
    assert counts["generate"] == 1, "双击必须只发一次生成 POST"
    # 选定 → 卡二出现已选定、版本高亮
    home.click("#rev-cards [data-action=select]")
    home.wait_for_selector("#rev-cards .rev-version.selected", state="visible")
    assert "已选定" in home.inner_text("#rev-cards")
    assert "正在" not in home.inner_text("#rev-cards")
    # 牵连检查 → findings 渲染
    home.click("#rev-cards [data-action=impact]")
    home.wait_for_selector("#rev-cards .rev-finding", state="visible")
    body = home.inner_text("#rev-cards")
    assert "导出的报告会包含您选定的方案" in body, "B7-3b 已交付，选定后报告承诺成立"
    assert counts["select"] == 1 and counts["impact"] == 1


def test_rev_cards_degraded_shows_retry_then_success(home):
    """业务失败链：degraded 如实显示 + 重试带消耗提示；重试成功恢复。"""
    phase, counts = _stub_rev_flow(home)
    phase["generate_result"] = "degraded"
    home.locator("#item-list .item.attention").first.click()
    home.wait_for_selector("#rev-cards [data-action=generate]", state="visible")
    home.click("#rev-cards [data-action=generate]")
    home.wait_for_function(
        "document.querySelector('#rev-cards') && "
        "document.querySelector('#rev-cards').innerText.includes("
        "'这次没有生成成功')"
    )
    body = home.inner_text("#rev-cards")
    assert "消耗一次可用次数" in body, "重试按钮必须带消耗提示"
    assert "正在生成" not in body, "失败返回后 busy 同样必须复位（复批 1）"
    # 重试成功
    phase["generate_result"] = "ok"
    home.click("#rev-cards [data-action=generate]")
    home.wait_for_selector("#rev-cards .rev-version", state="visible")
    assert counts["generate"] == 2


def test_rev_cards_late_failure_does_not_overwrite(home):
    """复批 5：可控延迟复现迟到响应——A 条目 state 挂起，切到 B 渲染后
    再放行 A 的迟到载荷（带独有标记），B 的卡片不得被覆盖。
    用 200+标记载荷而非 500：非 2xx 会打浏览器 console error 触发
    js_errors 门禁；seq 守卫在所有分支之前，200/5xx 走同一条丢弃路径。"""
    held = []

    def _hold(route):
        held.append(route)  # 暂不 fulfill：请求挂起

    phase, _ = _stub_rev_flow(home, second_attention=True)
    late_marker = "A条目迟到载荷-不应出现"
    home.route(
        "**/api/review/e2er00001/items/sublet/revision/state",
        lambda route: held.append(route),  # 暂不 fulfill：请求挂起
    )
    home.locator("#item-list .item.attention").first.click()  # = sublet
    home.wait_for_timeout(300)  # sublet state 挂起中
    assert "正在读取" in home.inner_text("#rev-cards")
    # 切到第二条（deposit）：立即渲染空态
    home.locator("#item-list .item.attention").nth(1).click()
    home.wait_for_selector("#rev-cards [data-action=generate]", state="visible")
    # A 的迟到响应到达（若守卫失效会渲染出带标记的草案卡）
    held[0].fulfill(
        status=200, content_type="application/json",
        body=json.dumps({"status": "ok", "item_id": "sublet",
                         "drafts": [{"draft_id": "d-late",
                                     "created_at": 1730000000,
                                     "status": "ok", "is_selected": False,
                                     "selected_stance": None,
                                     "versions": [{"stance": "稳妥",
                                                   "text": late_marker,
                                                   "note": ""}],
                                     "rationale": "", "assumptions": [],
                                     "declaration": ""}],
                         "selected": None, "impact": None,
                         "remaining": {"revision": 4, "impact": 3}},
                        ensure_ascii=False),
    )
    home.wait_for_timeout(400)
    assert home.is_visible("#rev-cards [data-action=generate]"), (
        "迟到响应不得覆盖当前条目卡片")
    assert late_marker not in home.inner_text("#rev-cards")
    assert "正在读取" not in home.inner_text("#rev-cards")
    assert phase["state"] == "empty"  # B 的空态原样


def test_rev_cards_late_body_after_headers_does_not_overwrite(home):
    """二轮复批 P1（正文时间窗）：A 的响应头先到、正文挂起；切到 B 后
    再放行 A 的正文——B 的卡片不得被覆盖、上下文不得被写成 A 的。
    用 ReadableStream 真实拉宽「头到/正文未完」窗口（route.fulfill 做不到）。"""
    phase, _ = _stub_rev_flow(home, second_attention=True)
    marker = "迟到正文载荷-不应出现"
    home.evaluate(
        """() => {
          const orig = window.fetch;
          window.fetch = (url, opts) => {
            if (String(url).includes('/items/sublet/revision/state')) {
              const stream = new ReadableStream({
                start(c) {
                  window.__releaseA = () => {
                    c.enqueue(new TextEncoder().encode(
                      JSON.stringify(window.__payloadA)));
                    c.close();
                  };
                },
              });
              return Promise.resolve(new Response(stream, {status: 200,
                headers: {'Content-Type': 'application/json'}}));
            }
            return orig(url, opts);
          };
          window.__payloadA = {status: 'ok', item_id: 'sublet',
            drafts: [{draft_id: 'd-late', created_at: 1730000000,
              status: 'ok', is_selected: false, selected_stance: null,
              versions: [{stance: '稳妥', text: '""" + marker + """', note: ''}],
              rationale: '', assumptions: [], declaration: ''}],
            selected: {draft_id: 'd-late', stance: '稳妥'},
            impact: null, remaining: {revision: 4, impact: 3}};
        }"""
    )
    home.locator("#item-list .item.attention").first.click()  # sublet：头到正文挂起
    home.wait_for_timeout(300)
    assert "正在读取" in home.inner_text("#rev-cards")
    # 切到 deposit：空态立即渲染
    home.locator("#item-list .item.attention").nth(1).click()
    home.wait_for_selector("#rev-cards [data-action=generate]", state="visible")
    # 此时 A 的正文才读完：二次校验必须丢弃
    home.evaluate("window.__releaseA()")
    home.wait_for_timeout(400)
    assert home.is_visible("#rev-cards [data-action=generate]"), (
        "迟到正文不得覆盖当前条目卡片")
    assert marker not in home.inner_text("#rev-cards")
    assert "正在读取" not in home.inner_text("#rev-cards")


def test_rev_cards_post_not_found_shows_read_fail_layer(pw_browser, base_url):
    """POST 404（记录已失效）→ 读取失败层三分的 gone 态。
    用不受 js_errors 守卫的裸 page：故意打 404 必产生 console error，
    守卫会把「故意模拟失败」误判为前端错误。"""
    page, ctx, _ = _new_page(pw_browser)
    page._e2e_base = base_url  # type: ignore[attr-defined]
    try:
        _goto_home(page)
        phase, _ = _stub_rev_flow(page)
        phase["generate_status"] = 404
        page.locator("#item-list .item.attention").first.click()
        page.wait_for_selector("#rev-cards [data-action=generate]",
                               state="visible")
        page.click("#rev-cards [data-action=generate]")
        page.wait_for_selector("#rev-cards [data-action=back]",
                               state="visible")
        assert "已过期或不存在" in page.inner_text("#rev-cards")
    finally:
        ctx.close()


# ---------- B7-5b 桌面双栏 + 问清楚 dialog（≥1024 专用；<1024=现状守护带） ----------

_DESKTOP_MIN_QUALITY = {
    "available": True, "reason": None,
    "observations": [
        {"dimension": "completeness", "title": "缺交付验收安排",
         "quote": "第四条 本合同一式两份", "clause_id": None,
         "comment": "异议期过短，建议延长。", "needs_confirm": True},
    ],
    "disclaimer": "", "dropped_count": 0, "coverage": None,
}


def _stub_desktop_findings_flow(page, findings):
    """带自定义 ai_findings 的假 done 记录（桌面用例：↻ 条件化/跳转）。"""
    import json as _json

    stub = _quality_review_stub(None)
    stub["ai_findings"] = {"available": True, "findings": findings}
    stub["ask_available"] = True
    page.route(
        "**/api/upload",
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({"review_id": "e2eq00001", "message": "uploaded", "status": "uploaded"}),
        ),
    )
    page.route(
        "**/api/review/e2eq00001",
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps(stub, ensure_ascii=False),
        ),
    )
    page.route(
        "**/api/review/e2eq00001/items/*/revision/state",
        lambda route: route.fulfill(
            status=200, content_type="application/json", body="{}"),
    )
    page.click("#entry-file")
    page.click("#entry-file")
    page.set_input_files("#file", str(FIXTURE))
    page.click("#btn-upload")
    page.wait_for_selector("#results-body", state="visible", timeout=15000)


def _finding(**overrides):
    f = {
        "finding_key": "obs:completeness:stub", "source_type": "observ+question",
        "fact_type": True, "dimension": "completeness",
        "title": "缺交付验收", "comment": "c", "quote": "第四条 本合同一式两份",
        "clause_id": None, "verification": None, "evidence": None,
        "status": "confirmed", "check_state": None, "question_id": "vq01",
        "rule_link": "", "rule_item_id": None,
        "materialized": False, "item_id": None, "item_status": None,
    }
    f.update(overrides)
    return f


def test_desktop_two_column_detail_in_side(desktop_home):
    """双栏：点条目 → #item-detail 搬进右栏 aside 且带详情头；未选时右栏空态。"""
    _stub_quality_flow(desktop_home, _DESKTOP_MIN_QUALITY)
    assert desktop_home.is_visible("#side-empty")
    desktop_home.click("#item-list .item")
    inside = desktop_home.eval_on_selector(
        ".col-side", "el => !!el.querySelector('#item-detail')")
    assert inside, "≥1024 时 #item-detail 应搬进右栏 aside"
    assert desktop_home.is_visible("#item-detail")
    assert not desktop_home.is_visible("#side-empty")
    # 详情头自带身份（名 + 档位 tag）
    head_text = desktop_home.inner_text("#detail-head")
    assert "转租限制" in head_text
    assert "需关注" in head_text


def test_tablet_band_detail_inline(home):
    """平板带（<1024）=现状守护带：详情留在左栏原位、右栏空态永不出现。"""
    _stub_quality_flow(home, _DESKTOP_MIN_QUALITY)
    home.click("#item-list .item")
    assert home.eval_on_selector(
        ".col-main", "el => !!el.querySelector('#item-detail')")
    assert not home.is_visible("#side-empty")


def test_desktop_ask_workbench_flow(desktop_home):
    """B7-6 对话工作台（弹窗已退役）：问答归档进共享流；详情/三卡原样在。"""
    import json as _json

    _stub_quality_flow(desktop_home, _DESKTOP_MIN_QUALITY, ask_available=True)
    desktop_home.route("**/api/ask", lambda route: route.fulfill(
        status=200, content_type="application/json",
        body=_json.dumps({"ok": True, "item_id": "sublet",
                          "answer": {"问题是啥": "工作台答案标记DLG9"},
                          "quote_verified": True}, ensure_ascii=False)))
    desktop_home.click("#item-list .item")
    desktop_home.click("#detail-ask")
    # 工作台常驻：交互卡可操作、详情仍在右栏
    assert desktop_home.is_visible("#ask-workbench .ask-interact")
    assert desktop_home.is_visible("#item-detail")
    desktop_home.fill("#ask-input", "这条怎么改？")
    desktop_home.click("#btn-ask")
    desktop_home.wait_for_selector("#ask-thread .ask-msg", timeout=8000)
    # 问答归档进共享流：气泡带「关于」标签+四段式答案
    bubble = desktop_home.inner_text("#ask-thread .ask-msg")
    assert "关于：" in bubble
    assert "工作台答案标记DLG9" in bubble
    # 回答槽位已清空（下一轮用）
    assert desktop_home.inner_text("#ask-answer") == ""


def test_desktop_ask_close_midflight_discards_late_answer(desktop_home):
    """B7-6 共享线程语义：发送中切到乙条目 → 甲的迟到旧响应不得写入
    乙的上下文（seq 判废；服务端按 turn 归档给甲，互不串写）。"""
    held = []
    desktop_home.route("**/api/ask", lambda route: held.append(route))
    # 双需关注 stub：甲乙都可追问（乙=early_termination 临时改档）
    stub2x = _quality_review_stub(None)
    stub2x["ask_available"] = True
    for it in stub2x["items"]:
        if it["id"] == "early_termination":
            it["status"] = "需关注"
    import json as _json
    desktop_home.route(
        "**/api/upload",
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({"review_id": "e2eq00001", "message": "uploaded", "status": "uploaded"}),
        ),
    )
    desktop_home.route(
        "**/api/review/e2eq00001",
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps(stub2x, ensure_ascii=False),
        ),
    )
    desktop_home.route(
        "**/api/review/e2eq00001/items/*/revision/state",
        lambda route: route.fulfill(
            status=200, content_type="application/json", body="{}"),
    )
    desktop_home.click("#entry-file")
    desktop_home.set_input_files("#file", str(FIXTURE))
    desktop_home.click("#btn-upload")
    desktop_home.wait_for_selector("#results-body", state="visible", timeout=15000)
    desktop_home.locator("#item-list .item").first.click()  # 甲：sublet
    desktop_home.click("#detail-ask")
    desktop_home.fill("#ask-input", "问题")
    desktop_home.click("#btn-ask")
    desktop_home.wait_for_timeout(400)
    assert desktop_home.eval_on_selector("#btn-ask", "el => el.disabled"),         "请求在途时发送按钮应禁用"
    # 发送中切到乙条目：fillAskContext 换条目 → seq+1 判废旧响应
    desktop_home.locator("#item-list .item").nth(1).click()
    desktop_home.click("#detail-ask")
    assert not desktop_home.eval_on_selector("#btn-ask", "el => el.disabled"),         "切条目后按钮应复位（旧响应已判废）"
    # 甲的迟到旧响应：seq 不匹配 → 不得写进乙的工作台
    held.pop().fulfill(
        status=200, content_type="application/json",
        body=_json.dumps({"ok": True, "item_id": "sublet",
                          "answer": {"问题是啥": "迟到旧答案STALE7"}},
                         ensure_ascii=False))
    desktop_home.wait_for_timeout(600)
    assert "STALE7" not in desktop_home.inner_text("#ask-workbench")


def test_desktop_recheck_entry_conditional(desktop_home):
    """≥1024：confirmed+摘句对得上 无再核入口；低置信结论 出「再核一次」。"""
    findings = [
        _finding(title="核对过的发现", verification="verified",
                 finding_key="obs:completeness:v", question_id="vq01"),
        _finding(title="没把握的发现", verification="unverified",
                 finding_key="obs:completeness:u", question_id="vq02"),
    ]
    _stub_desktop_findings_flow(desktop_home, findings)
    panel = desktop_home.inner_text("#ai-findings-panel")
    assert "再核一次（耗 1 次余量）" in panel, "低置信条目应有再核链接"
    links = desktop_home.query_selector_all(
        "#ai-findings-panel .verify-actions .evidence-jump")
    assert len(links) == 1, "结论「摘句对得上」的条目不应有再核入口"


def test_tablet_recheck_icon_unchanged(home):
    """<1024 保留现状：↻ 图标入口不受桌面条件化影响（codex 七轮 P2）。"""
    findings = [
        _finding(title="核对过的发现", verification="verified",
                 finding_key="obs:completeness:v", question_id="vq01"),
        _finding(title="没把握的发现", verification="unverified",
                 finding_key="obs:completeness:u", question_id="vq02"),
    ]
    _stub_desktop_findings_flow(home, findings)
    icons = home.query_selector_all("#ai-findings-panel .verify-btn-recheck")
    assert len(icons) == 2, "平板带现状：confirmed 条目 ↻ 图标照旧"
    assert "再核一次（耗 1 次余量）" not in home.inner_text("#ai-findings-panel")


def test_desktop_finding_jump_to_rule_item(desktop_home):
    """C3 跨源跳转：关联规则/已列入清单可点 → 跳到对应清单条目并选中。"""
    findings = [
        _finding(title="与违约金约定相关", verification="verified",
                 finding_key="obs:completeness:r", question_id="vq01",
                 rule_link="关联规则：转租限制", rule_item_id="sublet",
                 materialized=True, item_id="sublet", item_status="需关注"),
    ]
    _stub_desktop_findings_flow(desktop_home, findings)
    desktop_home.click("#ai-findings-panel .finding-jump >> nth=0")
    # 跳到规则条目：右栏详情头换成「转租限制」
    desktop_home.wait_for_timeout(300)
    assert "转租限制" in desktop_home.inner_text("#detail-head")


# ---------- B7-5b codex 九轮整改回归（六处） ----------

def test_ask_badge_keeps_source_label(home):
    """codex 九轮 P2-6：问答页状态行=档位 tag+来源徽章，不是两个重复档位。"""
    _stub_quality_flow(home, _DESKTOP_MIN_QUALITY)
    home.click("#item-list .item")
    home.click("#detail-ask")  # <1024：跳问答页
    home.wait_for_selector("#ask-workbench .ask-interact", state="visible")
    tags = home.query_selector_all("#ask-status-line .tag")
    assert len(tags) == 1, "状态行只应有一个档位 tag"
    assert "系统核查" in home.inner_text("#ask-status-line"), "来源徽章不得丢失"


def test_desktop_workbench_thread_restores_on_refresh(desktop_home):
    """B7-6 对话工作台：刷新后历史流从持久层恢复（含所属条目标签）。"""
    import json as _json

    stub = _quality_review_stub(None)
    stub["ai_findings"] = {"available": True, "findings": []}
    stub["ask_available"] = True
    stub["ask_thread"] = {"turns": [
        {"turn_no": 1, "request_id": "r1", "item_id": "sublet",
         "item_name": "转租限制", "state": "success",
         "question": "历史问题标记HIS5",
         "answer": {"问题是啥": "历史答案标记HIS6"}, "quote_verified": True},
    ]}
    desktop_home.route(
        "**/api/upload",
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps({"review_id": "e2eq00001", "message": "uploaded", "status": "uploaded"}),
        ),
    )
    desktop_home.route(
        "**/api/review/e2eq00001",
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps(stub, ensure_ascii=False),
        ),
    )
    desktop_home.route(
        "**/api/review/e2eq00001/items/*/revision/state",
        lambda route: route.fulfill(
            status=200, content_type="application/json", body="{}"),
    )
    desktop_home.click("#entry-file")
    desktop_home.set_input_files("#file", str(FIXTURE))
    desktop_home.click("#btn-upload")
    desktop_home.wait_for_selector("#results-body", state="visible", timeout=15000)
    assert "历史问题标记HIS5" in desktop_home.inner_text("#ask-thread")
    assert "历史答案标记HIS6" in desktop_home.inner_text("#ask-thread")
    assert "关于：转租限制" in desktop_home.inner_text("#ask-thread")


def test_workbench_follows_breakpoint(home):
    """B7-6（codex 十二轮 P2-5）：对话块随断点搬迁——桌面 col-side、
    平板带 col-main（三卡之后）；两个尺寸都可用。"""
    _stub_quality_flow(home, _DESKTOP_MIN_QUALITY, ask_available=True)
    home.click("#item-list .item")
    # 900px：对话块在 col-main
    assert home.eval_on_selector(
        ".col-main", "el => !!el.querySelector('#ask-workbench')")
    home.set_viewport_size({"width": 1280, "height": 900})
    home.wait_for_timeout(400)
    assert home.eval_on_selector(
        ".col-side", "el => !!el.querySelector('#ask-workbench')")
    # 详情也在右栏；工作台在详情之下
    assert home.eval_on_selector(
        ".col-side", "el => !!el.querySelector('#item-detail')")
    home.set_viewport_size({"width": 900, "height": 900})
    home.wait_for_timeout(400)
    assert home.eval_on_selector(
        ".col-main", "el => !!el.querySelector('#ask-workbench')")


def test_recheck_entry_refreshes_on_breakpoint_change(home):
    """codex 九轮 P2-2：1023↔1024 双向切换，再核入口随宽度刷新。"""
    findings = [
        _finding(title="没把握的发现", verification="unverified",
                 finding_key="obs:completeness:u", question_id="vq02"),
    ]
    _stub_desktop_findings_flow(home, findings)  # 900px 平板带
    assert home.query_selector_all("#ai-findings-panel .verify-btn-recheck")
    home.set_viewport_size({"width": 1280, "height": 900})
    home.wait_for_timeout(400)
    assert not home.query_selector_all("#ai-findings-panel .verify-btn-recheck"), \
        "拉宽后 ↻ 图标应被条件化文字链接替换"
    assert "再核一次（耗 1 次余量）" in home.inner_text("#ai-findings-panel")
    home.set_viewport_size({"width": 900, "height": 900})
    home.wait_for_timeout(400)
    assert home.query_selector_all("#ai-findings-panel .verify-btn-recheck"), \
        "缩回平板带 ↻ 图标应恢复"


def test_rev_excerpt_jump_flashes_excerpt_keeps_clause(home):
    """codex 九轮 P2-3：牵连「看原文」=flash 对应摘录；当前条目的
    「所在条款」身份提示不被改写。"""
    import json as _json

    _stub_quality_flow(home, _DESKTOP_MIN_QUALITY)
    home.route("**/api/review/e2eq00001/items/*/revision/state",
               lambda route: route.fulfill(
                   status=200, content_type="application/json",
                   body=_json.dumps({
                       # 摘录卡有「未选定前置闸」：必须带选定且与 impact.bound
                       # 匹配，ok 分支才渲染摘录
                       "selected": {"draft_id": "d1", "stance": "平衡"},
                       "impact": {"state": "ok",
                                  "bound": {"draft_id": "d1", "stance": "平衡"},
                                  "excerpts": [{"index": 0, "keyword": ["转租"],
                                                "position": 0, "clause_id": "c01",
                                                "excerpt": "第一条 租金与转租相关约定全文摘录。"}],
                                  "findings": [{"index": 0, "note": "说明一"}],
                                  "hidden_findings": 0, "hidden_indexes": [],
                                  "notice": ""}},
                       ensure_ascii=False)))
    home.click("#item-list .item")  # sublet：c04 → 「所在条款：第四条 房屋使用」
    home.wait_for_selector("#rev-cards:not(.hidden)", state="visible")
    clause_before = home.inner_text("#detail-clause")
    home.click("#rev-cards [data-rev-jump]")
    home.wait_for_timeout(200)
    assert home.eval_on_selector(
        "[data-rev-excerpt]", "el => el.classList.contains('is-flash')"), \
        "应 flash 对应摘录文本"
    clause_after = home.inner_text("#detail-clause")
    assert clause_after == clause_before, "当前条目的条款身份提示不得被改写"


def test_back_and_review_switch_closes_ask_context(desktop_home):
    """codex 十轮 P1（B7-6 版）：Back／跨合同切换必须清问答上下文
    （askItem 清空→迟到旧响应拒写），杜绝甲合同问题发给乙合同。"""
    import json as _json

    _stub_quality_flow(desktop_home, _DESKTOP_MIN_QUALITY, ask_available=True)
    held = []
    desktop_home.route("**/api/ask", lambda route: held.append(route))
    # 打开工作台并真正发送一个问题（请求挂起——codex 十一轮补强）
    desktop_home.locator("#item-list .item").first.click()
    desktop_home.click("#detail-ask")
    desktop_home.fill("#ask-input", "甲合同的问题")
    desktop_home.click("#btn-ask")
    desktop_home.wait_for_timeout(300)
    assert held, "追问请求应已发出且被挂起"
    assert desktop_home.eval_on_selector("#btn-ask", "el => el.disabled"),         "请求在途时发送按钮应禁用"
    # 跨合同切换（hash 到另一 rid；stub 供第二份合同）→ 上下文必须清场
    stub2 = _quality_review_stub(None)
    stub2["id"] = "e2ez00002"
    stub2["filename"] = "乙合同.txt"
    desktop_home.route(
        "**/api/review/e2ez00002",
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=_json.dumps(stub2, ensure_ascii=False)))
    desktop_home.route(
        "**/api/review/e2ez00002/items/*/revision/state",
        lambda route: route.fulfill(
            status=200, content_type="application/json", body="{}"))
    desktop_home.evaluate("window.location.hash = '#/review/e2ez00002'")
    desktop_home.wait_for_timeout(500)
    assert not desktop_home.eval_on_selector("#btn-ask", "el => el.disabled"),         "跨合同切换必须复位问答会话"
    # 迟到旧响应不得写入（askItem 已清，双保险拒写）
    assert held, "被挂起的旧请求此刻才放行"
    held.pop().fulfill(
        status=200, content_type="application/json",
        body=_json.dumps({"ok": True, "item_id": "sublet",
                          "answer": {"问题是啥": "跨合同迟到STALE9"}},
                         ensure_ascii=False))
    desktop_home.wait_for_timeout(400)
    assert "STALE9" not in desktop_home.inner_text("body")


# ---- codex 十五轮整改：断点退抽屉 / 提交态重算 / 断线恢复 / 切回同步 ----

def test_drawer_closes_when_viewport_widens(mobile_home):
    """codex 十五轮 P2-7：抽屉开着拉宽过断点 → 同步退抽屉（否则桌面
    无汉堡可点、inert 罩死工作区，交互死路）。"""
    mobile_home.click("#shell-menu")
    mobile_home.wait_for_selector("#shell-nav.is-open", state="attached")
    mobile_home.set_viewport_size({"width": 1280, "height": 800})
    mobile_home.wait_for_function(
        "!document.getElementById('shell-nav').classList.contains('is-open')")
    assert not mobile_home.eval_on_selector(
        "#shell-main", "el => !!el.inert"), "桌面工作区不得残留 inert"


def test_submit_reenabled_after_paste_overlimit_switch(home):
    """codex 十五轮 P2-8：粘贴超限禁用提交后切回文件入口 → 重算可用。"""
    home.click("#entry-paste")
    home.wait_for_selector("#paste-wrap", state="visible")
    home.eval_on_selector(
        "#paste-text",
        "el => { el.value = 'x'.repeat(200001);"
        " el.dispatchEvent(new Event('input', {bubbles: true})); }")
    assert home.eval_on_selector("#btn-upload", "el => el.disabled")
    home.click("#entry-file")
    assert not home.eval_on_selector(
        "#btn-upload", "el => el.disabled"), "切回文件入口须重新启用提交"


def test_failed_ask_survives_reload_with_same_request_id(home):
    """codex 十五轮 P1-3：断线失败 → request_id 落 localStorage；刷新后
    恢复卡回填原问题，重发沿用原编号（服务端去重不二次计费）。"""
    import json as _json
    sent_request_ids: list[str] = []
    fail_first = {"v": True}

    def ask_route(route):
        body = _json.loads(route.request.post_data or "{}")
        sent_request_ids.append(body.get("request_id") or "")
        if fail_first["v"]:
            route.fulfill(status=200, content_type="application/json",
                          body=_json.dumps({"ok": False, "item_id": "sublet",
                                            "item_name": "转租限制",
                                            "error": "连接中断"}, ensure_ascii=False))
        else:
            route.fulfill(status=200, content_type="application/json",
                          body=_json.dumps(
                              {"ok": True, "item_id": "sublet", "item_name": "转租限制",
                               "answer": {"问题是啥": "恢复成功ANS"},
                               "raw_text": None, "error": None}, ensure_ascii=False))

    _stub_ask_flow(home, {})
    home.unroute("**/api/ask")
    home.route("**/api/ask", ask_route)
    home.locator("#item-list .item.attention").first.click()
    home.click("#detail-ask")
    home.wait_for_selector("#ask-workbench .ask-interact", state="visible")
    home.fill("#ask-input", "断线重试的问题")
    home.click("#btn-ask")
    home.wait_for_selector("#ask-error:not(.hidden)", state="visible")
    stored = home.evaluate("localStorage.getItem('ask-pending-v1')")
    assert stored, "失败后 request_id 必须落 localStorage"
    orig_id = _json.loads(stored)["request_id"]
    assert _json.loads(stored)["question"] == "断线重试的问题"

    # 刷新：routes 绑定随 page 存活；localStorage 留存；
    # 清 hash 回空态（hash 还挂着会让 resumeFromHash 直达结果态）
    home.reload()
    home.evaluate("location.hash = ''")
    home.wait_for_selector("#entry-file", state="visible", timeout=10000)
    _stub_ask_flow(home, {})
    home.unroute("**/api/ask")
    home.route("**/api/ask", ask_route)
    home.locator("#item-list .item.attention").first.click()
    home.click("#detail-ask")
    home.wait_for_selector("#ask-workbench .ask-interact", state="visible")
    # 恢复卡出现 → 点击回填原问题
    home.wait_for_selector("#ask-pending:not(.hidden)", state="visible")
    assert "断线重试的问题" in home.inner_text("#ask-pending")
    home.click("#ask-pending button")
    assert home.input_value("#ask-input") == "断线重试的问题"
    # 重发：同编号；成功后 pending 清空
    fail_first["v"] = False
    home.click("#btn-ask")
    home.wait_for_selector("#ask-thread .ask-msg", state="visible")
    assert sent_request_ids[-1] == orig_id, "重发必须沿用原 request_id"
    assert home.evaluate("localStorage.getItem('ask-pending-v1')") is None


def test_switch_back_item_syncs_archived_answer(home):
    """codex 十五轮 P2-4：甲在途回答期间切乙（守卫拒写），切回甲 →
    按持久化轮次同步共享流，无需整页刷新。"""
    stub2x = _quality_review_stub(None)
    stub2x["ask_available"] = True
    for it in stub2x["items"]:
        if it["id"] == "early_termination":
            it["status"] = "需关注"
    import json as _json
    held: list = []

    def hold_ask(route):
        held.append(route)  # 挂起不 fulfill = 可控延迟（在册技巧）

    home.route("**/api/upload", lambda r: r.fulfill(
        status=200, content_type="application/json",
        body=_json.dumps({"review_id": "e2eq00001", "message": "uploaded",
                          "status": "uploaded"})))
    # 动态 stub：真实服务端在归档后 GET 才带 ask_thread——fulfill 答案的
    # 同时落档进 stub，模拟「迟到响应已归档在服务端」
    def review_route(r):
        r.fulfill(status=200, content_type="application/json",
                  body=_json.dumps(stub2x, ensure_ascii=False))
    home.route("**/api/review/e2eq00001", review_route)
    home.route("**/api/review/e2eq00001/items/*/revision/state", lambda r: r.fulfill(
        status=200, content_type="application/json",
        body=_json.dumps({"status": "ok", "item_id": "sublet", "drafts": [],
                          "selected": None, "impact": None,
                          "remaining": {"revision": 4, "impact": 3}})))
    home.route("**/api/ask", hold_ask)
    home.click("#entry-file")
    home.set_input_files("#file", str(FIXTURE))
    home.click("#btn-upload")
    home.wait_for_selector("#results-body", state="visible", timeout=15000)
    home.locator("#item-list .item").first.click()  # 甲：sublet
    home.click("#detail-ask")
    home.fill("#ask-input", "甲的在途问题")
    home.click("#btn-ask")
    home.wait_for_timeout(300)
    # 发送中切乙
    home.locator("#item-list .item").nth(1).click()
    home.click("#detail-ask")
    # 甲的迟到响应归档（页面守卫拒写——此刻不该出现）
    held.pop().fulfill(
        status=200, content_type="application/json",
        body=_json.dumps({"ok": True, "item_id": "sublet", "item_name": "转租限制",
                          "answer": {"问题是啥": "甲的归档答案SYNC"},
                          "raw_text": None, "error": None}, ensure_ascii=False))
    stub2x["ask_thread"] = {"turns": [{
        "turn_no": 1, "request_id": "sync-1", "item_id": "sublet",
        "item_name": "转租限制", "state": "success",
        "question": "甲的在途问题", "created_at": 1.0,
        "answer": {"问题是啥": "甲的归档答案SYNC"}, "quote_verified": True}]}
    home.wait_for_timeout(400)
    assert "甲的归档答案SYNC" not in home.inner_text("#ask-thread")
    # 切回甲：openItemAsk 触发 syncThreadFromServer（纯读，无需刷新）
    home.locator("#item-list .item").first.click()
    home.click("#detail-ask")
    home.wait_for_selector("#ask-thread .ask-msg", state="visible", timeout=10000)
    assert "甲的在途问题" in home.inner_text("#ask-thread .ask-msg-q")
    assert "甲的归档答案SYNC" in home.inner_text("#ask-thread")


def test_pending_saved_before_send_survives_reload(home):
    """codex 十六轮 P1-1：请求在途（未收到回执）关标签再重开 → 恢复卡
    接得上（身份在发送之前落盘，不靠失败回调）。"""
    import json as _json
    home.route("**/api/ask", lambda route: None)  # 挂起不 fulfill = 模拟在途
    _stub_ask_flow(home, {})
    home.unroute("**/api/ask")
    home.route("**/api/ask", lambda route: None)
    home.locator("#item-list .item.attention").first.click()
    home.click("#detail-ask")
    home.wait_for_selector("#ask-workbench .ask-interact", state="visible")
    home.fill("#ask-input", "在途关标签的问题")
    home.click("#btn-ask")
    home.wait_for_timeout(500)
    stored = home.evaluate("localStorage.getItem('ask-pending-v1')")
    assert stored, "发送前就必须落盘（在途无回执也要能恢复）"
    assert _json.loads(stored)["question"] == "在途关标签的问题"
    # 模拟重开：刷新 + 清 hash
    home.reload()
    home.evaluate("location.hash = ''")
    home.wait_for_selector("#entry-file", state="visible", timeout=10000)
    _stub_ask_flow(home, {})
    home.locator("#item-list .item.attention").first.click()
    home.click("#detail-ask")
    home.wait_for_selector("#ask-pending:not(.hidden)", state="visible")
    assert "在途关标签的问题" in home.inner_text("#ask-pending")


def test_processing_receipt_shows_waiting_then_result(home):
    """codex 十六轮 P1-2：processing 去重回执（ok+deduped 无答案）→
    保留编号+对话流显示「正在回答…」，轮询到最终结果后渲染并清编号；
    全程不得渲染「暂无」假答案。"""
    import json as _json
    stub = _quality_review_stub(None)
    stub["ask_available"] = True
    turn = {
        "turn_no": 1, "request_id": "proc-1", "item_id": "sublet",
        "item_name": "转租限制", "state": "success",
        "question": "处理中的问题", "created_at": 1.0,
        "answer": {"问题是啥": "轮询拿到答案PROC"},
        "raw_text": None, "quote_verified": True,
    }
    gets = {"n": 0}
    sent = {"request_id": None}

    def ask_route(r):
        # 客户端生成幂等键——归档轮必须回显同一个编号（服务端按
        # (review_id, request_id) 去重，轮询按编号找 turn）
        sent["request_id"] = (_json.loads(r.request.post_data or "{}")
                              ).get("request_id")
        r.fulfill(status=200, content_type="application/json",
                  body=_json.dumps({"ok": True, "deduped": True,
                                    "item_id": "sublet", "item_name": "转租限制",
                                    "turn_no": 1}, ensure_ascii=False))

    def review_route(r):
        gets["n"] += 1
        payload = dict(stub)
        if gets["n"] > 4 and sent["request_id"]:
            # 头几拍读档无归档轮（模拟模型还在跑）——延后两拍让「正在回答」
            # 等待态稳定可断言，之后带上同编号归档轮
            archived = dict(turn, request_id=sent["request_id"])
            payload["ask_thread"] = {"turns": [archived]}
        r.fulfill(status=200, content_type="application/json",
                  body=_json.dumps(payload, ensure_ascii=False))

    home.route("**/api/upload", lambda r: r.fulfill(
        status=200, content_type="application/json",
        body=_json.dumps({"review_id": "e2eq00001", "message": "uploaded",
                          "status": "uploaded"})))
    home.route("**/api/review/e2eq00001", review_route)
    home.route("**/api/review/e2eq00001/items/*/revision/state", lambda r: r.fulfill(
        status=200, content_type="application/json",
        body=_json.dumps({"status": "ok", "item_id": "sublet", "drafts": [],
                          "selected": None, "impact": None,
                          "remaining": {"revision": 4, "impact": 3}})))
    home.route("**/api/ask", ask_route)
    home.click("#entry-file")
    home.set_input_files("#file", str(FIXTURE))
    home.click("#btn-upload")
    home.wait_for_selector("#results-body", state="visible", timeout=15000)
    home.locator("#item-list .item.attention").first.click()
    home.click("#detail-ask")
    home.fill("#ask-input", "处理中的问题")
    home.click("#btn-ask")
    # 等待态出现（不是「暂无」假答案）
    home.wait_for_selector("#ask-thread .ask-msg-waiting", state="visible")
    assert "正在回答" in home.inner_text("#ask-thread .ask-msg-waiting")
    assert home.evaluate("localStorage.getItem('ask-pending-v1')") is not None, \
        "处理中不得清编号"
    # 轮询到最终结果：等待态移除、真答案渲染、编号清空
    home.wait_for_selector("#ask-thread .ask-msg .answer", state="visible",
                           timeout=12000)
    home.wait_for_selector("#ask-thread .ask-msg-waiting", state="detached",
                           timeout=5000)
    assert "轮询拿到答案PROC" in home.inner_text("#ask-thread")
    assert home.evaluate("localStorage.getItem('ask-pending-v1')") is None


def test_old_poll_success_keeps_newer_pending_request_id(home):
    """codex 十七轮 P1：甲轮询成功只清自己的恢复记录——乙问题在途时，
    甲的结果落地不得清掉乙的编号（否则关标签丢同编号重试）。"""
    import json as _json
    stub = _quality_review_stub(None)
    stub["ask_available"] = True
    sent: list[str] = []

    def ask_route(r):
        rid_req = (_json.loads(r.request.post_data or "{}")).get("request_id")
        sent.append(rid_req)
        # 甲乙都回处理中回执（等待态），由轮询取终局
        r.fulfill(status=200, content_type="application/json",
                  body=_json.dumps({"ok": True, "deduped": True,
                                    "item_id": "sublet", "item_name": "转租限制"},
                                   ensure_ascii=False))

    def review_route(r):
        payload = dict(stub)
        if len(sent) >= 2 and sent[0]:
            # 只有甲的终局归档；乙保持处理中（轮询继续但不影响断言）
            archived = {"turn_no": 1, "request_id": sent[0],
                        "item_id": "sublet", "item_name": "转租限制",
                        "state": "success", "question": "甲的问题",
                        "created_at": 1.0,
                        "answer": {"问题是啥": "甲的结果OLDPOLL"},
                        "raw_text": None, "quote_verified": True}
            payload["ask_thread"] = {"turns": [archived]}
        r.fulfill(status=200, content_type="application/json",
                  body=_json.dumps(payload, ensure_ascii=False))

    home.route("**/api/upload", lambda r: r.fulfill(
        status=200, content_type="application/json",
        body=_json.dumps({"review_id": "e2eq00001", "message": "uploaded",
                          "status": "uploaded"})))
    home.route("**/api/review/e2eq00001", review_route)
    home.route("**/api/review/e2eq00001/items/*/revision/state", lambda r: r.fulfill(
        status=200, content_type="application/json",
        body=_json.dumps({"status": "ok", "item_id": "sublet", "drafts": [],
                          "selected": None, "impact": None,
                          "remaining": {"revision": 4, "impact": 3}})))
    home.route("**/api/ask", ask_route)
    home.click("#entry-file")
    home.set_input_files("#file", str(FIXTURE))
    home.click("#btn-upload")
    home.wait_for_selector("#results-body", state="visible", timeout=15000)
    home.locator("#item-list .item.attention").first.click()
    home.click("#detail-ask")
    home.fill("#ask-input", "甲的问题")
    home.click("#btn-ask")
    home.wait_for_selector("#ask-thread .ask-msg-waiting", state="visible")
    # 乙问题紧随发出：恢复编号已换成乙
    home.fill("#ask-input", "乙的问题")
    home.click("#btn-ask")
    home.wait_for_selector("#ask-thread .ask-msg-waiting", state="visible",
                           timeout=10000)
    assert home.eval_on_selector_all(
        "#ask-thread .ask-msg-waiting", "els=>els.length") == 2
    pending_now = _json.loads(
        home.evaluate("localStorage.getItem('ask-pending-v1')"))
    assert pending_now["request_id"] == sent[1], "恢复编号应已是乙"
    # 甲轮询到成功：等待气泡剩乙一条；恢复记录必须还是乙的；
    # 期间发过乙（seq 已进位）→ 请求守卫拦下旧轮询的现场写入
    home.wait_for_function(
        "document.querySelectorAll('#ask-thread .ask-msg-waiting').length === 1",
        timeout=12000)
    pending_after = _json.loads(
        home.evaluate("localStorage.getItem('ask-pending-v1')") or "null")
    assert pending_after is not None, "甲的成功不得清掉乙的恢复编号"
    assert pending_after["request_id"] == sent[1]
    assert "甲的结果OLDPOLL" not in home.inner_text("#ask-thread")
    # 切走再切回：syncThreadFromServer 按持久化轮次补渲甲的答案
    home.locator("#item-list .item").nth(1).click()
    home.locator("#item-list .item").first.click()
    home.click("#detail-ask")
    home.wait_for_selector("#ask-thread .ask-msg .answer", state="visible",
                           timeout=10000)
    assert "甲的结果OLDPOLL" in home.inner_text("#ask-thread")
