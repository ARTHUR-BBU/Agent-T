# -*- coding: utf-8 -*-
"""B7-6 C0 后端测试：历史列表 / 粘贴文本模式 / 对话工作台持久化。

codex 十二/十三/十四轮规则逐条落测：粘贴互斥与超限不截短、50 轮上限
调模型前检查、幂等键（丢失重发/同键并发只一条记录一次调用）、归档按
turn（谁的归谁）。零付费：llm_ask.ask_about_item 全程 monkeypatch。
"""
from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import llm_ask
from app.services.store import ReviewStore

client = TestClient(app)


@pytest.fixture()
def env_store(tmp_path, monkeypatch):
    db = ReviewStore(db_path=str(tmp_path / "t.db"))
    monkeypatch.setattr("app.api.routes.store", db)
    return db


def _ok_result():
    return {"ok": True, "answer": {"问题是啥": "答案标记ANS3"},
            "raw_text": "", "quote_verified": True}


def _mk_review(db: ReviewStore, filename: str) -> str:
    return db.create(filename=filename, category="procurement")


def test_paste_mode_upload_and_validation(env_store):
    """附录 B：粘贴直传同权入库；互斥/空白/超限独立校验。"""
    r = client.post("/api/upload", data={
        "pasted_text": "第一条 本合同为粘贴测试合同文本，内容超过两百字以示正式。" * 3,
        "category": "procurement",
    })
    assert r.status_code == 200, r.text
    rid = r.json()["review_id"]
    row = env_store.get(rid)
    assert row["filename"].startswith("粘贴文本_")
    # 互斥
    r2 = client.post("/api/upload", data={
        "pasted_text": "x", "category": "procurement"},
        files={"file": ("a.txt", b"abc", "text/plain")})
    assert r2.status_code == 422
    # 两者皆无
    r3 = client.post("/api/upload", data={"category": "procurement"})
    assert r3.status_code == 400
    # 空白
    r4 = client.post("/api/upload", data={"pasted_text": "   ", "category": "procurement"})
    assert r4.status_code == 400
    # 超限：200,001 字符 → 413（不截短续审）
    r5 = client.post("/api/upload", data={
        "pasted_text": "甲" * 200_001, "category": "procurement"})
    assert r5.status_code == 413


def test_reviews_list_summary(env_store):
    db = env_store
    r1 = db.create(filename="采购A.txt", category="procurement")
    db.update(r1, status="done")
    r2 = db.create(filename="租赁B.txt", category="lease")
    got = client.get("/api/reviews")
    assert got.status_code == 200
    items = got.json()["reviews"]
    ids = [x["id"] for x in items]
    assert r1 in ids and r2 in ids
    one = next(x for x in items if x["id"] == r1)
    assert one["filename"] == "采购A.txt" and one["status"] == "done"
    # 摘要字段口径：不回合同文本/结果载荷
    assert "text" not in one and "items" not in one
    # 服务端限条
    got2 = client.get("/api/reviews?limit=1")
    assert len(got2.json()["reviews"]) == 1


def _seed_askable_review(db: ReviewStore) -> str:
    rid = db.create(filename="c.txt", category="procurement")
    db.update(rid, status="done", text="第四条 违约金为合同总价款的30%。",
              items=[{"id": "penalty", "name": "违约金上限", "status": "需关注",
                      "note": "n", "quote": "违约金为合同总价款的30%", "hits": []}])
    return rid


def test_ask_thread_persist_and_dedupe(env_store, monkeypatch):
    """codex 十三/十四轮：turn 持久化+幂等键（成功重发不二次调用）。"""
    db = env_store
    rid = _seed_askable_review(db)
    calls = {"n": 0}

    def fake_ask(**kwargs):
        calls["n"] += 1
        return _ok_result()

    monkeypatch.setattr(llm_ask, "ask_about_item", fake_ask)
    r1 = client.post("/api/ask", json={
        "review_id": rid, "item_id": "penalty",
        "question": "违约金 30% 合理吗？", "request_id": "req-1"})
    assert r1.status_code == 200
    body = r1.json()
    assert body["ok"] and body["turn_no"] == 1 and body["deduped"] is False
    assert calls["n"] == 1
    # 同 request_id 重发（首次响应丢失场景）：返回存档答案，不二次调用
    r2 = client.post("/api/ask", json={
        "review_id": rid, "item_id": "penalty",
        "question": "违约金 30% 合理吗？", "request_id": "req-1"})
    assert r2.json()["deduped"] is True
    assert r2.json()["answer"] == {"问题是啥": "答案标记ANS3"}
    assert calls["n"] == 1
    # 新 request_id = 新 turn
    r3 = client.post("/api/ask", json={
        "review_id": rid, "item_id": "penalty",
        "question": "那 20% 呢？", "request_id": "req-2"})
    assert r3.json()["turn_no"] == 2 and calls["n"] == 2
    # GET review 带出对话流（刷新恢复）
    got = client.get(f"/api/review/{rid}")
    turns = got.json()["ask_thread"]["turns"]
    assert [t["turn_no"] for t in turns] == [1, 2]
    assert turns[0]["state"] == "success"
    assert turns[0]["question"].startswith("违约金")


def test_ask_failed_turn_retry_reuses_same_turn(env_store, monkeypatch):
    """失败轮重试=原 turn 状态流转，绝不新开 turn（重试不重复入账）。"""
    db = env_store
    rid = _seed_askable_review(db)
    calls = {"n": 0}

    def fake_ask(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"ok": False, "error": "模型超时"}
        return _ok_result()

    monkeypatch.setattr(llm_ask, "ask_about_item", fake_ask)
    r1 = client.post("/api/ask", json={
        "review_id": rid, "item_id": "penalty", "question": "q1",
        "request_id": "req-f1"})
    assert r1.json()["ok"] is False
    turns = db.get(rid)["ask_thread"]["turns"]
    assert len(turns) == 1 and turns[0]["state"] == "failed"
    # 同 request_id 重试：原 turn 复活流转成功
    r2 = client.post("/api/ask", json={
        "review_id": rid, "item_id": "penalty", "question": "q1",
        "request_id": "req-f1"})
    assert r2.json()["ok"] is True and calls["n"] == 2
    turns = db.get(rid)["ask_thread"]["turns"]
    assert len(turns) == 1 and turns[0]["state"] == "success"


def test_ask_turn_limit_checked_before_model(env_store, monkeypatch):
    """codex 十二轮：50 轮上限在调模型之前检查——超限零调用、零费用。"""
    db = env_store
    rid = _seed_askable_review(db)
    db.update(rid, ask_thread={"turns": [
        {"turn_no": i, "request_id": f"seed-{i}", "item_id": "penalty",
         "item_name": "违约金上限", "state": "success", "question": "q",
         "created_at": 0.0}
        for i in range(1, 51)
    ]})
    calls = {"n": 0}

    def fake_ask(**kwargs):
        calls["n"] += 1
        return _ok_result()

    monkeypatch.setattr(llm_ask, "ask_about_item", fake_ask)
    r = client.post("/api/ask", json={
        "review_id": rid, "item_id": "penalty", "question": "q51",
        "request_id": "req-51"})
    assert r.json()["ok"] is False
    assert "上限" in (r.json()["error"] or "")
    assert calls["n"] == 0, "超限必须在调模型之前拒绝（零费用）"
    assert len(db.get(rid)["ask_thread"]["turns"]) == 50, "不产生半截记录"


def test_ask_processing_dedupe_and_archive_isolation(env_store, monkeypatch):
    """codex 十四轮验收②：处理中同键并发 → 返回状态不再调模型；
    甲的答案归甲的 turn（归档与显示解耦的服务端半场）。"""
    db = env_store
    rid = _seed_askable_review(db)
    release = threading.Event()
    started = threading.Event()

    def slow_ask(**kwargs):
        started.set()
        release.wait(timeout=10)
        return _ok_result()

    monkeypatch.setattr(llm_ask, "ask_about_item", slow_ask)

    errors: list[str] = []

    def send():
        try:
            r = client.post("/api/ask", json={
                "review_id": rid, "item_id": "penalty", "question": "甲的问题",
                "request_id": "req-A"})
            assert r.status_code == 200
        except Exception as exc:  # noqa: BLE001
            errors.append(str(exc))

    t = threading.Thread(target=send)
    t.start()
    started.wait(timeout=5)
    # 等服务端把 turn 落成 processing
    for _ in range(50):
        turns = (db.get(rid).get("ask_thread") or {}).get("turns") or []
        if turns and turns[-1].get("state") == "processing":
            break
        threading.Event().wait(0.1)
    # 处理中同键并发：返回状态、不再调用（slow_ask 不重入）
    r2 = client.post("/api/ask", json={
        "review_id": rid, "item_id": "penalty", "question": "甲的问题",
        "request_id": "req-A"})
    assert r2.json()["deduped"] is True
    release.set()
    t.join(timeout=10)
    assert not errors, errors
    turns = db.get(rid)["ask_thread"]["turns"]
    assert len(turns) == 1 and turns[0]["state"] == "success"
    assert turns[0]["item_id"] == "penalty"
