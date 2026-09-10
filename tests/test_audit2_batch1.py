"""外部审计批 1 整改测试：流式上传限额、解析闸门、ask 条款上下文。

对应「代码质量审查报告｜第一轮」P1 三项（报告 §二/§三/§四）。
"""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

from app.main import app
from app.services import llm_ask
from app.services.clause_index import build_clause_index, build_clause_context

client = TestClient(app)

_MINI = "甲方乙方约定：货款验收合格后支付。争议向法院起诉。适用中华人民共和国法律。"


# ---------- 批1-② 流式限额 ----------

def test_upload_reads_in_chunks_not_whole_file():
    """流式结构断言：分块粒度存在且为 MB 级（整读=审计指出的内存放大路径）。"""
    from app.api import routes

    assert 64 * 1024 <= routes._READ_CHUNK <= 1024 * 1024, "分块读取粒度必须存在且有限"


def test_10mb_plus_one_still_413():
    """既有业务限制不回退（外部审计原测试只验证了这一点，保持）。"""
    from app.api import routes

    big = b"x" * (routes.MAX_UPLOAD_BYTES + 1)
    r = client.post(
        "/api/upload",
        files={"file": ("big.txt", big, "text/plain")},
        data={"category": "procurement"},
    )
    assert r.status_code == 413


def test_normal_size_upload_still_works():
    files = {"file": ("mini.txt", _MINI.encode("utf-8"), "text/plain")}
    assert client.post("/api/upload", files=files, data={"category": "procurement"}).status_code == 200


# ---------- 批1-② 解析闸门 ----------

def test_parse_slots_exist_and_bounded():
    from app.api import routes

    assert routes._parse_slots is not None
    # 非阻塞语义：acquire(blocking=False) 满时返 False（不挂事件循环）
    assert hasattr(routes._parse_slots, "acquire")


def test_precheck_skipped_when_parse_slots_full(monkeypatch):
    """解析闸门占满 → 跳过预审照常开审（可用性优先，对齐 precheck busy）。"""
    monkeypatch.setenv("PRECHECK_ENABLED", "true")
    precheck_called = {"n": 0}
    monkeypatch.setattr(
        "app.api.routes.precheck_service.run_precheck",
        lambda *a, **kw: precheck_called.update(n=precheck_called["n"] + 1) or _skip_outcome(),
    )
    from app.api import routes

    # 占满全部解析槽
    holders = []
    for _ in range(routes.MAX_CONCURRENT_PARSES):
        assert routes._parse_slots.acquire(blocking=False)
        holders.append(True)
    try:
        files = {"file": ("mini.txt", _MINI.encode("utf-8"), "text/plain")}
        r = client.post("/api/upload", files=files, data={"category": "procurement"})
        assert r.status_code == 200
        assert r.json()["review_id"], "闸门满时照常开审（预审降级跳过）"
        assert precheck_called["n"] == 0, "解析槽满时预审不应被调用"
    finally:
        for _ in holders:
            routes._parse_slots.release()


def _skip_outcome():
    from app.services.precheck import PrecheckOutcome

    return PrecheckOutcome(skip_reason="busy")


# ---------- 批1-③ ask 条款上下文 ----------

def _numbered_doc() -> str:
    return (
        "第一条 标的\n采购办公设备若干。\n"
        "第二条 价款\n含税总价拾万元整。\n"
        "第三条 付款方式\n甲方在收到业主支付款项后再向乙方付款。\n"
        "第四条 违约责任\n逾期按日千分之一计违约金。\n"
        "第五条 争议解决\n向甲方所在地法院起诉。\n"
    )


def test_build_clause_context_returns_hit_and_neighbors():
    text = _numbered_doc()
    idx = build_clause_index(text)
    ctx = build_clause_context(text, idx, ["c03"])  # 第三条 付款方式
    assert "第三条 付款方式" in ctx
    assert "业主支付款项" in ctx, "命中条款全文必须进上下文"
    assert "第二条 价款" in ctx and "第四条 违约责任" in ctx, "相邻条款必须进上下文"
    assert "第五条" not in ctx, "不相邻条款不进（控制延迟）"


def test_build_clause_context_caps_total_size():
    text = "第一条 甲\n" + "内容。" * 3000 + "\n第二条 乙\n尾部。"
    idx = build_clause_index(text)
    ctx = build_clause_context(text, idx, ["c01"], max_chars=2000)
    assert len(ctx) <= 2000


def test_build_clause_context_fallbacks():
    text = _numbered_doc()
    idx = build_clause_index(text)
    # 未命中 clause_ids / 空索引 / None 容错 → 空串（调用方回退头尾采样）
    assert build_clause_context(text, idx, []) == ""
    assert build_clause_context(text, {"clauses": []}, ["c01"]) == ""
    assert build_clause_context(text, idx, ["c99"]) == ""
    assert build_clause_context(text, {"clauses": [{"id": "c01", "start": None, "end": 5}]}, ["c01"]) == ""


def test_ask_user_prompt_uses_clause_context_when_available():
    item = {"name": "付款", "id": "payment", "status": "需关注", "note": "n", "quote": "q"}
    prompt = llm_ask._build_user_prompt(
        "怎么改？", item, "全文" * 3000,
        clause_context="第三条 付款方式\n甲方在收到业主支付款项后再向乙方付款。",
    )
    assert "条款上下文" in prompt
    assert "业主支付款项" in prompt
    assert "改写以此为准" in prompt
    assert "合同整体背景" in prompt, "头尾采样仍作为全局背景保留"


def test_ask_user_prompt_falls_back_without_context():
    """无条款上下文（旧记录/未定位）→ prompt 形状与历史版本一致。"""
    item = {"name": "付款", "id": "payment", "status": "需关注", "note": "n", "quote": "q"}
    prompt = llm_ask._build_user_prompt("怎么改？", item, "全文内容。")
    assert "条款上下文" not in prompt
    assert "合同全文" in prompt
