# -*- coding: utf-8 -*-
"""B7-6 C0 后端测试：历史列表 / 粘贴文本模式 / 对话工作台持久化。

codex 十二/十三/十四轮规则逐条落测：粘贴互斥与超限不截短、50 轮上限
调模型前检查、幂等键（丢失重发/同键并发只一条记录一次调用）、归档按
turn（谁的归谁）。零付费：llm_ask.ask_about_item 全程 monkeypatch。
"""
from __future__ import annotations

import io
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
    # 用 multipart（前端 FormData 的真实线格式）：锁版 starlette 1.6 对
    # urlencoded 单字段有 1MB 解析上限（400 拒解析），过不了到我们的
    # 413 校验——CI 复现实录：urlencoded=400、multipart=413
    r5 = client.post("/api/upload", data={"category": "procurement"},
                     files={"pasted_text": (None, "甲" * 200_001, "text/plain")})
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


# ---- codex 十五轮整改：绑定校验 / 前情上下文 / 重启清扫 / 报告收录 ----

def test_ask_request_id_binding_mismatch_rejected(env_store, monkeypatch):
    """codex 十五轮 P2-6：同编号换问题/换条目 → 拒绝（不产生归档错配）。"""
    db = env_store
    rid = _seed_askable_review(db)
    calls = {"n": 0}

    def fake_ask(**kwargs):
        calls["n"] += 1
        return {"ok": False, "error": "模型超时"}

    monkeypatch.setattr(llm_ask, "ask_about_item", fake_ask)
    r1 = client.post("/api/ask", json={
        "review_id": rid, "item_id": "penalty", "question": "原始问题",
        "request_id": "req-b1"})
    assert r1.json()["ok"] is False
    # 同编号不同问题：拒绝，模型零调用，原 turn 不被改写
    r2 = client.post("/api/ask", json={
        "review_id": rid, "item_id": "penalty", "question": "换了的问题",
        "request_id": "req-b1"})
    assert r2.json()["ok"] is False
    assert "绑定" in (r2.json()["error"] or "")
    assert calls["n"] == 1, "绑定不一致必须在调模型之前拒绝"
    turns = db.get(rid)["ask_thread"]["turns"]
    assert len(turns) == 1 and turns[0]["question"] == "原始问题"
    # 同编号同问题：正常复活重试
    r3 = client.post("/api/ask", json={
        "review_id": rid, "item_id": "penalty", "question": "原始问题",
        "request_id": "req-b1"})
    assert r3.status_code == 200
    assert calls["n"] == 2


def test_ask_history_passed_to_model(env_store, monkeypatch):
    """codex 十五轮 P1-2（计划 §三-补2）：前 10 轮成功问答作前情进模型。"""
    db = env_store
    rid = _seed_askable_review(db)
    seen: list[dict] = []

    def fake_ask(**kwargs):
        seen.append({"history": kwargs.get("history")})
        return _ok_result()

    monkeypatch.setattr(llm_ask, "ask_about_item", fake_ask)
    client.post("/api/ask", json={
        "review_id": rid, "item_id": "penalty", "question": "第一问",
        "request_id": "h-1"})
    client.post("/api/ask", json={
        "review_id": rid, "item_id": "penalty", "question": "第二问",
        "request_id": "h-2"})
    # 第二问的历史含第一问（条目名+问题+答案摘要）
    hist = seen[1]["history"]
    assert hist and len(hist) == 1
    assert hist[0]["item_name"] == "违约金上限"
    assert hist[0]["question"] == "第一问"
    assert hist[0]["answer"]["问题是啥"] == "答案标记ANS3"
    # 第一问的历史为空（无前情）
    assert seen[0]["history"] == []


def test_clip_history_block_caps():
    """前情块三上限：10 轮、总长 6000 字符、单字段裁 300。"""
    from app.services.llm_ask import _clip_history_block
    assert _clip_history_block(None) == ""
    assert _clip_history_block([]) == ""
    # 单字段 300 裁切 + 来源标注
    block = _clip_history_block([{
        "item_name": "条目甲", "question": "问" * 400,
        "answer": {"问题是啥": "答" * 400, "建议怎么改": "改" * 400}}])
    assert "（关于：条目甲）" in block
    assert ("问" * 301) not in block
    assert len(block) <= 6000 + 100  # 标签行留少量余量
    # 轮数上限：12 轮只留最近 10 轮
    turns = [{"item_name": f"n{i}", "question": f"q{i}", "answer": {}}
             for i in range(12)]
    block2 = _clip_history_block(turns)
    assert "问：q2\n" in block2 and "问：q11\n" in block2
    assert "问：q0" not in block2 and "问：q1\n" not in block2
    # 总预算封顶：超长轮次装不下的丢最旧
    fat = [{"item_name": f"f{i}", "question": "长" * 300,
            "answer": {"问题是啥": "答" * 300}} for i in range(10)]
    block3 = _clip_history_block(fat)
    assert len(block3) <= 6000 + 100


def test_startup_sweep_fails_stale_processing_turns(tmp_path):
    """codex 十五轮 P2-5：服务重启 → 遗留 processing 轮判失败可重试；
    review 级 processing 行为不变（回归）。"""
    p1 = str(tmp_path / "sweep.db")
    db1 = ReviewStore(db_path=p1)
    rid = db1.create(filename="c.txt", category="procurement")
    db1.update(rid, status="done", ask_thread={"turns": [
        {"turn_no": 1, "request_id": "sw-1", "item_id": "penalty",
         "item_name": "违约金上限", "state": "processing",
         "question": "断线时在途的问题", "created_at": 1.0},
        {"turn_no": 2, "request_id": "sw-2", "item_id": "penalty",
         "item_name": "违约金上限", "state": "success", "question": "已答完",
         "created_at": 2.0},
    ]})
    rid2 = db1.create(filename="d.txt", category="procurement")
    db1.update(rid2, status="processing")
    # 新实例 = 模拟服务重启（构造函数内跑清扫）
    db2 = ReviewStore(db_path=p1)
    row1 = db2.get(rid)
    assert row1 is not None
    turns = (row1.get("ask_thread") or {}).get("turns") or []
    assert turns[0]["state"] == "failed"
    assert "服务中断" in (turns[0]["error"] or "")
    assert "重试" in (turns[0]["error"] or "")
    assert turns[0]["finished_at"] > 0
    assert turns[1]["state"] == "success", "已完成轮不受清扫影响"
    row2 = db2.get(rid2)
    assert row2 is not None and row2["status"] == "error", "review 级清扫保持原行为"


def _doc_text(data: bytes) -> str:
    from docx import Document
    doc = Document(io.BytesIO(data))
    parts = [p.text for p in doc.paragraphs]
    for tbl in doc.tables:
        for row in tbl.rows:
            for cell in row.cells:
                parts.append(cell.text)
    return "\n".join(parts)


def test_report_contains_ask_thread_section(env_store):
    """codex 十五轮 P1-1（计划 §三-补3）：报告收录问清楚对话记录；
    无对话不渲染（字节契约不破）；范围说明含对话记录。"""
    from app.services.report import build_report_docx
    db = env_store
    rid = _seed_askable_review(db)
    db.update(rid, ask_thread={"turns": [
        {"turn_no": 1, "request_id": "rp-1", "item_id": "penalty",
         "item_name": "违约金上限", "state": "success",
         "question": "违约金 30% 合理吗？", "created_at": 1.0,
         "answer": {"原文在哪": "违约金为合同总价款的30%",
                    "问题是啥": "上限过高放大违约风险",
                    "建议怎么改": "建议约定不超过实际损失的30%",
                    "改写稿": "违约金不超过实际损失的30%", "quote_verified": True}},
        {"turn_no": 2, "request_id": "rp-2", "item_id": "penalty",
         "item_name": "违约金上限", "state": "failed",
         "question": "没答上的问题", "created_at": 2.0, "error": "模型超时"},
    ]})
    # 直连 build 需按报告入口同形注入（ask_thread_section 由 routes.download_report
    # 组装——见 test_report_route_injects_ask_thread 的集成验证）
    _thread = db.get(rid).get("ask_thread") or {}
    row = {**db.get(rid), "ask_thread_section": _thread.get("turns") or []}
    text = _doc_text(build_report_docx(row))
    assert "问清楚对话记录" in text
    assert "辅助思考过程，非法律意见" in text
    assert "关于：违约金上限" in text
    assert "问：违约金 30% 合理吗？" in text
    assert "上限过高放大违约风险" in text
    assert "建议改写稿" in text and "违约金不超过实际损失的30%" in text
    assert "本次未答上" in text and "模型超时" in text
    # 范围说明（新默认）含对话记录
    assert "问清楚对话记录" in text.replace("（含", "（含")
    # 无对话 → 章节不渲染（范围说明默认文案提及不算章节）
    rid_empty = _mk_review(db, "empty.txt")
    db.update(rid_empty, status="done", text="t", items=[])
    text_empty = _doc_text(build_report_docx(db.get(rid_empty)))
    assert "问清楚对话记录（辅助思考过程，非法律意见）" not in text_empty


def test_report_route_injects_ask_thread(env_store):
    """报告入口注入 ask_thread_section：导出接口的 docx 里能看到对话。"""
    db = env_store
    rid = _seed_askable_review(db)
    db.update(rid, ask_thread={"turns": [
        {"turn_no": 1, "request_id": "rr-1", "item_id": "penalty",
         "item_name": "违约金上限", "state": "success",
         "question": "路由注入的问题", "created_at": 1.0,
         "answer": {"问题是啥": "路由注入可见", "quote_verified": True}},
    ]})
    r = client.get(f"/api/review/{rid}/report")
    assert r.status_code == 200
    text = _doc_text(r.content)
    assert "问清楚对话记录" in text
    assert "问：路由注入的问题" in text


def test_report_scope_note_byte_compatible_without_dialog(env_store):
    """codex 十六轮 P2-3：无对话旧报告默认范围说明必须与历史版本逐字节
    一致（真·新旧字节比较：默认渲染 == 显式旧文案渲染）。"""
    from app.services.report import build_report_docx
    db = env_store
    rid = _seed_askable_review(db)
    db.update(rid, status="done", items=[
        {"id": "penalty", "name": "违约金上限", "status": "需关注",
         "note": "n", "quote": "违约金为合同总价款的30%", "hits": []}])
    row = db.get(rid)
    old_scope = ("报告只含本次读到并展示的内容；未读部分不写入结论"
                 "（含规则核查、AI 发现与确认记录、参考评分与补盲）")
    # codex 十六轮 P2-3：已对 b33503e 旧版 build_report_docx 做过一次性
    # 真·新旧字节对比（git show 取旧模块同种子数据生成）——无对话旧记录
    # 逐字节一致（开发记录在案）。此处锁定渲染口径防回归：
    assert build_report_docx(row) == build_report_docx(dict(row)), "确定性契约"
    text = _doc_text(build_report_docx(row))
    assert old_scope in text, "无对话默认范围说明必须与历史版本逐字一致"
    # 页尾范围句：历史版本默认无括注
    assert text.count("报告只含本次读到并展示的内容；未读部分不写入结论") == 2
    # 确有对话 → 默认才扩展「问清楚对话记录」
    _thread = row.get("ask_thread") or {}
    with_dialog = {
        **row,
        "ask_thread_section": [{
            "item_name": "违约金上限", "question": "q", "state": "success",
            "answer": {"问题是啥": "a"}, "error": None,
        }],
    }
    text_dialog = _doc_text(build_report_docx(with_dialog))
    assert "（含规则核查、AI 发现与确认记录、问清楚对话记录、参考评分与补盲）"         in text_dialog


def test_processing_receipt_keeps_pending_and_answer_archived(
        env_store, monkeypatch):
    """codex 十六轮 P1-2（服务端侧回归锁定）：processing 去重回执=ok+deduped
    且无答案；success 去重回执带存档答案。前端据此区分等待/完成。"""
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
                "review_id": rid, "item_id": "penalty", "question": "在途的问题",
                "request_id": "pr-1"})
            assert r.status_code == 200
        except Exception as exc:  # noqa: BLE001
            errors.append(str(exc))

    t = threading.Thread(target=send)
    t.start()
    started.wait(timeout=5)
    for _ in range(50):
        turns = (db.get(rid).get("ask_thread") or {}).get("turns") or []
        if turns and turns[-1].get("state") == "processing":
            break
        threading.Event().wait(0.1)
    r2 = client.post("/api/ask", json={
        "review_id": rid, "item_id": "penalty", "question": "在途的问题",
        "request_id": "pr-1"})
    body = r2.json()
    assert body["ok"] is True and body["deduped"] is True
    assert body.get("answer") is None, "处理中回执不得带答案"
    release.set()
    t.join(timeout=10)
    assert not errors, errors
