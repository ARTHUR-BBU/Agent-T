"""stage 进度段位测试（阶段 0.2 补课）：回调顺序、API 进度、旧记录兼容、error 传播。"""
from __future__ import annotations

import time

from fastapi.testclient import TestClient

from app.api import routes
from app.main import app
from app.services.store import store

client = TestClient(app)

_MINI = "甲方乙方约定：货款验收合格后支付。争议向法院起诉。适用中华人民共和国法律。"


def _upload_mini() -> str:
    files = {"file": ("mini.txt", _MINI.encode("utf-8"), "text/plain")}
    r = client.post("/api/upload", files=files, data={"category": "procurement"})
    assert r.status_code == 200
    return r.json()["review_id"]


# ---------- pipeline 回调 ----------

def test_run_review_emits_stage_sequence(monkeypatch):
    from app.graph import pipeline

    monkeypatch.setattr(
        pipeline,
        "run_model_review",
        lambda **kw: {
            "scorecard": {"available": False, "reason": "no_llm_key"},
            "blind_candidates": [],
            "blind_skipped_messages": [],
            "blind_skipped_reason": None,
            "blind_enabled": False,
        },
    )
    stages: list[str] = []
    result = pipeline.run_review(
        "t.txt", _MINI.encode("utf-8"), category="procurement", on_stage=stages.append
    )
    assert stages == ["scanning", "scoring"], "解析入口=扫描、评分节点入口=评分"
    assert result["error"] == ""


def test_stage_callback_exception_never_breaks_review(monkeypatch):
    from app.graph import pipeline

    monkeypatch.setattr(
        pipeline,
        "run_model_review",
        lambda **kw: {
            "scorecard": {"available": False, "reason": "no_llm_key"},
            "blind_candidates": [],
            "blind_skipped_messages": [],
            "blind_skipped_reason": None,
            "blind_enabled": False,
        },
    )

    def bad_callback(_stage: str) -> None:
        raise RuntimeError("store 写入失败")

    result = pipeline.run_review(
        "t.txt", _MINI.encode("utf-8"), category="procurement", on_stage=bad_callback
    )
    assert result["error"] == "", "回调异常不得影响审查主流程"


# ---------- API 进度 ----------

def test_upload_stage_progress_reaches_done():
    rid = _upload_mini()
    seen: set[str | None] = set()
    deadline = time.time() + 15
    while time.time() < deadline:
        d = client.get(f"/api/review/{rid}").json()
        seen.add(d.get("stage"))
        if d["status"] != "processing":
            break
        time.sleep(0.1)
    assert d["status"] == "done"
    assert d["stage"] == "done", "终态 stage 必须为 done"
    assert seen & {"scanning", "scoring"}, f"轮询应至少观测到一个中间段位，实际 {seen}"


def test_worker_error_marks_stage_error(monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("模拟审查崩溃")

    monkeypatch.setattr(routes, "run_review", boom)
    rid = _upload_mini()
    deadline = time.time() + 10
    while time.time() < deadline:
        d = client.get(f"/api/review/{rid}").json()
        if d["status"] == "error":
            break
        time.sleep(0.1)
    assert d["status"] == "error"
    assert d["stage"] == "error", "失败态 stage 必须落 error（等待页能标出失败段）"


# ---------- 旧记录兼容 ----------

def test_legacy_record_without_stage_returns_none():
    """阶段 0.2 之前的老行没有 stage 键：API 返回 None，前端退化单行文案。"""
    rid = store.create(
        filename="old.txt",
        category="lease",
        category_label="lease",
        status="done",
        items=[],
        scorecard={},
    )
    d = client.get(f"/api/review/{rid}").json()
    assert d["stage"] is None
    assert d["status"] == "done"
