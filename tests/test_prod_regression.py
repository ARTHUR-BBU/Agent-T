# -*- coding: utf-8 -*-
"""生产并发对照工具 · 429 限频重试回归测试（外审 PR #103 修复卡）。

覆盖：429→200 读取并夹紧 Retry-After；五连 429 恰好 5 次请求只等待 4 次
（最后一次不空等）；非 429 立即返回；category 参数透传不写死。
requests.post / time.sleep 全程 monkeypatch，不发真实网络请求。
"""
from __future__ import annotations

import tools.m65.prod_regression as pr


class _Resp:
    def __init__(self, status_code: int, headers: dict | None = None,
                 text: str = ""):
        self.status_code = status_code
        self.headers = headers or {}
        self.text = text

    def json(self) -> dict:
        return {}


def test_429_then_200_reads_and_clamps_retry_after(monkeypatch) -> None:
    """429 后按 Retry-After 等待（超出上下限时夹紧 5-120s），再请求 200。"""
    posts: list[dict] = []
    sleeps: list[float] = []

    def fake_post(url, auth=None, files=None, data=None, timeout=None):
        posts.append({"data": data})
        # Retry-After=999（超上限）→ 必须夹到 120
        return _Resp(429 if len(posts) == 1 else 200,
                     headers={"Retry-After": "999"})

    monkeypatch.setattr(pr.requests, "post", fake_post)
    monkeypatch.setattr(pr.time, "sleep", lambda s: sleeps.append(s))

    r = pr._upload_with_retry("http://h", ("u", "p"), "f.docx",
                              "procurement", b"x")
    assert r.status_code == 200
    assert len(posts) == 2
    assert sleeps == [120.0], f"Retry-After 未夹紧上限: {sleeps}"


def test_retry_after_below_floor_is_raised(monkeypatch) -> None:
    """Retry-After=1（低于下限）→ 必须抬到 5s。"""
    posts: list[int] = []
    sleeps: list[float] = []

    def fake_post(url, auth=None, files=None, data=None, timeout=None):
        posts.append(1)
        return _Resp(429 if len(posts) == 1 else 200,
                     headers={"Retry-After": "1"})

    monkeypatch.setattr(pr.requests, "post", fake_post)
    monkeypatch.setattr(pr.time, "sleep", lambda s: sleeps.append(s))

    pr._upload_with_retry("http://h", ("u", "p"), "f.docx",
                          "procurement", b"x")
    assert sleeps == [5.0], f"Retry-After 未抬到下限: {sleeps}"


def test_five_429s_five_requests_only_four_sleeps(monkeypatch) -> None:
    """外审实锤缺口：五连 429 必须恰好 5 次请求、只等待 4 次——
    最后一次 429 后没有下一次请求，不得再按 Retry-After 空等。"""
    calls = {"post": 0}
    sleeps: list[float] = []

    def fake_post(url, auth=None, files=None, data=None, timeout=None):
        calls["post"] += 1
        return _Resp(429, headers={"Retry-After": "10"})

    monkeypatch.setattr(pr.requests, "post", fake_post)
    monkeypatch.setattr(pr.time, "sleep", lambda s: sleeps.append(s))

    r = pr._upload_with_retry("http://h", ("u", "p"), "f.docx",
                              "procurement", b"x")
    assert r.status_code == 429, "重试耗尽应返回最后一次 429"
    assert calls["post"] == 5, f"应恰好请求 5 次，实际 {calls['post']}"
    assert len(sleeps) == 4, (
        f"最后一次 429 后不得空等：应只 sleep 4 次，实际 {len(sleeps)}")


def test_non_429_returns_immediately(monkeypatch) -> None:
    """非 429（如 500）立即返回，不重试不等待。"""
    calls = {"post": 0, "sleep": 0}

    def fake_post(url, auth=None, files=None, data=None, timeout=None):
        calls["post"] += 1
        return _Resp(500, text="boom")

    monkeypatch.setattr(pr.requests, "post", fake_post)
    monkeypatch.setattr(pr.time, "sleep", lambda s: calls.__setitem__("sleep", calls["sleep"] + 1))

    r = pr._upload_with_retry("http://h", ("u", "p"), "f.docx",
                              "lease", b"x")
    assert r.status_code == 500
    assert calls["post"] == 1
    assert calls["sleep"] == 0


def test_category_passed_through(monkeypatch) -> None:
    """category 参数必须透传（不写死）——NDA/租赁件走各自品类对照。"""
    seen: list[dict] = []

    def fake_post(url, auth=None, files=None, data=None, timeout=None):
        seen.append(data or {})
        return _Resp(200)

    monkeypatch.setattr(pr.requests, "post", fake_post)
    monkeypatch.setattr(pr.time, "sleep", lambda s: None)

    pr._upload_with_retry("http://h", ("u", "p"), "nda.docx", "nda", b"x")
    assert seen[0].get("category") == "nda", f"category 未透传: {seen[0]}"
    assert seen[0].get("force") == "true"
