"""前端 E2E 测试（Playwright + Chromium，无 Key 模式）。

测试面：上传页 → 上传合同 → 结果渲染（清单/评分卡缺省态/报告导出）→
条目详情 → 追问入口（未开通横幅）→ 导航返回。全程无 LLM 调用，行为确定。

依赖：pip install playwright && playwright install chromium
未安装时本模块整体 skip，不影响其余测试套件。
"""
from __future__ import annotations

import os
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
    import httpx
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


@pytest.fixture()
def page(pw_browser):
    """每个用例独立 context + page；JS 异常/Console error 在 teardown 统一守护
    （小智娘 P2：只挂在单条用例里会让其余 15 条静默放过 JS 错误）。"""
    ctx = pw_browser.new_context()
    pg = ctx.new_page()
    js_errors: list[str] = []
    pg.on("pageerror", lambda e: js_errors.append(str(e)))
    pg.on("console", lambda m: js_errors.append(m.text) if m.type == "error" else None)
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


def _upload(page, path: Path = FIXTURE, category: str = "lease"):
    page.set_input_files("#file", str(path))
    page.select_option("#category", category)
    page.click("#btn-upload")
    # 审查同步完成，等结果体渲染出来
    page.wait_for_selector("#results-body", state="visible", timeout=15000)
    page.wait_for_selector(".item", state="visible", timeout=5000)


# ---------- 上传页 ----------

def test_home_renders_upload_screen(home):
    assert home.title() == "合同审查"
    assert home.is_visible("#screen-upload")
    assert not home.is_visible("#screen-results")
    options = home.eval_on_selector_all(
        "#category option", "els => els.map(e => [e.value, e.textContent])"
    )
    assert [v for v, _ in options] == ["procurement", "nda", "lease"]
    assert home.is_visible("#btn-upload")


def test_upload_without_file_shows_error(home):
    home.click("#btn-upload")
    home.wait_for_selector("#upload-error", state="visible")
    assert "请先选择合同文件" in home.inner_text("#upload-error")
    # 仍停留在上传页
    assert home.is_visible("#screen-upload")


def test_legend_shows_four_status_tags(home):
    tags = home.eval_on_selector_all(
        ".legend .tag", "els => els.map(e => e.textContent.trim())"
    )
    assert tags == ["通过", "需关注", "未找到", "不适用"]


# ---------- 完整审查流（租赁金标合同） ----------

def test_full_review_flow_renders_results(home):
    _upload(home)
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
    home.wait_for_selector("#screen-upload", state="visible")
    assert "#/review/" not in home.url, "返回上传页必须清掉旧审查 hash"


def test_attention_item_click_shows_detail_and_ask(home):
    _upload(home)
    home.locator("#item-list .item.attention").first.click()
    home.wait_for_selector("#item-detail", state="visible")
    assert "说明" in home.inner_text("#item-detail")
    # 需关注条目开放「问清楚一点」入口
    assert home.is_visible("#detail-actions")


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
    assert not home.is_visible("#score-card")
    note = home.locator(".score-unavailable")
    assert note.count() == 1
    assert "评分暂未开通" in note.inner_text()


# ---------- M4 报告导出 ----------

def test_export_report_button_and_download(home):
    _upload(home)
    assert home.is_visible("#report-actions")
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
    home.wait_for_selector("#screen-ask", state="visible")
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
    home.wait_for_selector("#screen-ask", state="visible")
    # 上下文与规则说明一致；采购金标需关注项带摘句，追问页原文应同步展示
    assert home.inner_text("#ask-context").strip() == note.strip()
    assert not home.locator("#ask-quote").evaluate("el => el.classList.contains('empty')")


# ---------- 导航 ----------

def test_back_navigation_ask_to_results(home):
    _upload(home)
    home.locator("#item-list .item.attention").first.click()
    home.click("#detail-ask")
    home.wait_for_selector("#screen-ask", state="visible")
    home.click("#btn-back-results")
    assert home.is_visible("#screen-results")
    assert not home.is_visible("#screen-ask")


def test_back_navigation_results_to_upload(home):
    _upload(home)
    home.click("#btn-back-upload")
    assert home.is_visible("#screen-upload")
    assert not home.is_visible("#screen-results")


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
    home.wait_for_selector("#screen-ask", state="visible")
    home.click("#btn-back-results")
    assert home.is_visible("#screen-results")
