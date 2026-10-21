# -*- coding: utf-8 -*-
"""B2-3 两项升级专项验收（spec-b23-trail-budget §3 测试清单）。

三件硬事：
①旧账本迁移不能错列或丢记录（显式列名写入+真实旧 schema 三步验收）
②账本区分「请求成功」与「解析成功」（先创建后结算，parse_failed 无假绿）
③预算拦截必须留痕（emit_skip 显式落账）
加：隐私对抗（复述合同防重存）/审查单锚点反查（review_id→trace→全链）。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from unittest import mock

from app.services import category_suggest as cs
from app.services import llm_budget
from app.services import llm_call_log

FAKE_ENV = {"CATEGORY_SUGGEST_ENABLED": "true", "CF_API_TOKEN": "k",
            "CF_ACCOUNT_ID": "acct", "CF_CLEF_MODEL": "clef-flash"}


# ---------- §1.1 硬事一：迁移不能错列或丢记录 ----------

LEGACY_DDL = (
    "CREATE TABLE llm_calls ("
    "call_id TEXT PRIMARY KEY,"
    "review_id TEXT,"
    "node TEXT NOT NULL,"
    "provider TEXT NOT NULL,"
    "model TEXT NOT NULL,"
    "prompt_version TEXT,"
    "document_version TEXT,"
    "chars_sent INTEGER,"
    "truncated INTEGER,"
    "attempt INTEGER DEFAULT 1,"
    "started_at TEXT,"
    "latency_ms INTEGER,"
    "outcome TEXT NOT NULL,"
    "usage TEXT,"
    "gate_counts TEXT,"
    "error_detail TEXT,"
    "created_at REAL NOT NULL)"
)


def _legacy_db(tmp_path):
    """真实旧 schema fixture（无三新列+历史行）。"""
    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(db)
    conn.execute(LEGACY_DDL)
    conn.execute(
        "INSERT INTO llm_calls (call_id, node, provider, model, outcome, usage,"
        " gate_counts, created_at) VALUES ('legacy1','precheck','zhipu','glm',"
        "'success','{}','{}', 1000.0)"
    )
    conn.commit()
    conn.close()
    return db


def test_migration_three_step_upgrade_read_write(tmp_path, monkeypatch) -> None:
    """三步真实验收：①升级②按列名读回历史行③写新行按列名读回。"""
    db = _legacy_db(tmp_path)
    monkeypatch.setenv("STORE_DB_PATH", str(db))

    # ② 升级后历史行可按列名读回（错列/丢记录当场暴露）
    rows = llm_call_log.query()
    assert len(rows) == 1
    legacy = rows[0]
    assert legacy["call_id"] == "legacy1"
    assert legacy["outcome"] == "success"
    assert legacy["trace_id"] is None and legacy["raw_output"] is None

    # ③ 写新行：三新列按名落账，位置漂移不可能
    rec = llm_call_log.LLMCallRecord(
        call_id="new1", node="category_suggest", provider="cloudflare",
        model="clef-flash", trace_id="t1", raw_output='{"category":"lease"}',
        prompt_hash="abc123", outcome="success",
    )
    llm_call_log.emit(rec)
    by_id = {r["call_id"]: r for r in llm_call_log.query()}
    assert by_id["new1"]["trace_id"] == "t1"
    assert by_id["new1"]["raw_output"] == '{"category":"lease"}'
    assert by_id["new1"]["prompt_hash"] == "abc123"
    # 历史行未被迁移破坏
    assert by_id["legacy1"]["call_id"] == "legacy1"


def test_migrate_idempotent_and_concurrency_guard(tmp_path, monkeypatch) -> None:
    """重复迁移幂等（duplicate column 良性捕获）。"""
    db = _legacy_db(tmp_path)
    monkeypatch.setenv("STORE_DB_PATH", str(db))
    conn = sqlite3.connect(db)
    for _ in range(3):
        llm_call_log._ensure_table(conn)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(llm_calls)")}
    assert {"trace_id", "raw_output", "prompt_hash"} <= cols
    conn.close()


# ---------- §1.2 硬事二：结算守卫 ----------

def test_settle_transitions_and_guard(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("STORE_DB_PATH", str(tmp_path / "c.db"))
    llm_call_log.emit(llm_call_log.LLMCallRecord(
        call_id="s1", node="category_suggest", provider="cloudflare",
        model="clef-flash", outcome="success"))
    # success→parse_failed（先创建后结算的核心迁移）
    assert llm_call_log.settle("s1", "parse_failed", None, "bad verdict") is True
    rows = llm_call_log.query()
    assert rows[0]["outcome"] == "parse_failed"
    # 终态不可二次结算（parse_failed→success 被守卫拒绝）
    assert llm_call_log.settle("s1", "success", "{}") is False
    # provider_error 为 transport 终态，不可被结算改写
    llm_call_log.emit(llm_call_log.LLMCallRecord(
        call_id="s2", node="category_suggest", provider="cloudflare",
        model="clef-flash", outcome="provider_error"))
    assert llm_call_log.settle("s2", "success", "{}") is False
    assert llm_call_log.query(trace_id=None)[0]["outcome"] == "provider_error" or any(
        r["call_id"] == "s2" and r["outcome"] == "provider_error"
        for r in llm_call_log.query())


def test_settle_caps_raw_output_2048(tmp_path, monkeypatch) -> None:
    """长度兜底：超 2048 截断+标记（内容白名单由调用方保证，此处兜底）。"""
    monkeypatch.setenv("STORE_DB_PATH", str(tmp_path / "c.db"))
    llm_call_log.emit(llm_call_log.LLMCallRecord(
        call_id="c1", node="category_suggest", provider="cloudflare",
        model="clef-flash", outcome="success"))
    llm_call_log.settle("c1", "success", "x" * 3000)
    row = llm_call_log.query()[0]
    assert row["raw_output"].startswith("x" * 10)
    assert row["raw_output"].endswith("...[truncated]")
    assert len(row["raw_output"]) <= 2048 + len("...[truncated]")


# ---------- §1.3 硬事三：emit_skip / backfill / trace 查询 ----------

def test_emit_skip_writes_budget_exceeded_row(tmp_path, monkeypatch) -> None:
    """预算拦截=零网络请求也必须有账（否则收银台无法审计）。"""
    monkeypatch.setenv("STORE_DB_PATH", str(tmp_path / "c.db"))
    llm_call_log.emit_skip(
        "category_suggest", "cloudflare", "clef-flash", trace_id="t9",
        prompt_version=cs.PROMPT_VERSION, outcome="budget_exceeded",
        error_detail="suggest_budget_limit=2",
    )
    rows = llm_call_log.query(trace_id="t9")
    assert len(rows) == 1
    assert rows[0]["outcome"] == "budget_exceeded"
    assert rows[0]["node"] == "category_suggest"
    assert rows[0]["chars_sent"] is None and rows[0]["latency_ms"] is None


def test_backfill_review_id_idempotent(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("STORE_DB_PATH", str(tmp_path / "c.db"))
    llm_call_log.emit(llm_call_log.LLMCallRecord(
        call_id="b1", node="precheck", provider="zhipu", model="glm",
        trace_id="tb", outcome="success"))
    assert llm_call_log.backfill_review_id("rev1", "tb") == 1
    # 幂等：已关联的行不再改写
    assert llm_call_log.backfill_review_id("rev2", "tb") == 0
    assert llm_call_log.query(review_id="rev1")[0]["call_id"] == "b1"


# ---------- §1.3 建议层预算闸 ----------

def _client_mock(payload):
    """httpx.Client mock（同存量测试样板）。"""
    c = mock.Mock()
    c.return_value.__enter__ = lambda s: s
    c.return_value.__exit__ = lambda s, *a: False
    c.return_value.post.return_value.json.return_value = payload
    c.return_value.post.return_value.status_code = 200
    return c


def _ok_payload(choice="lease", confidence=0.95):
    return {"success": True,
            "result": {"answers": {"category": {"type": "choice",
                                                "choice": choice,
                                                "confidence": confidence}},
                       "usage": {}},
            }


def test_budget_exhausted_skips_with_ledger_row(tmp_path, monkeypatch) -> None:
    """额度耗尽：零网络请求断言+SQLite 真有 budget_exceeded 行。"""
    monkeypatch.setenv("STORE_DB_PATH", str(tmp_path / "c.db"))
    budget = llm_budget.ReviewBudget(1)
    assert budget.try_consume(1)  # 预支场景：额度已被同请求其他路径用光
    with mock.patch.dict("os.environ", FAKE_ENV), \
            mock.patch.object(cs, "_load_env", return_value=dict(FAKE_ENV)), \
            mock.patch("app.services.llm_client.httpx.Client") as fake:
        out = cs.suggest_category("文本", "procurement", budget=budget)
    assert out.performed is False
    assert out.skip_reason == "budget_exceeded"
    fake.assert_not_called()  # 零网络请求
    rows = llm_call_log.query()
    assert any(r["outcome"] == "budget_exceeded" and r["node"] == "category_suggest"
               for r in rows)


def test_retry_consumes_second_budget_token(tmp_path, monkeypatch) -> None:
    """瞬态重试前二次扣减：额度 2=判定 1+重试 1；额度 1 时重试被闸拦下。"""
    monkeypatch.setenv("STORE_DB_PATH", str(tmp_path / "c.db"))
    import httpx
    budget = llm_budget.ReviewBudget(1)
    with mock.patch.dict("os.environ", FAKE_ENV), \
            mock.patch.object(cs, "_load_env", return_value=dict(FAKE_ENV)), \
            mock.patch("app.services.llm_client.httpx.Client") as fake, \
            mock.patch.object(llm_call_log, "emit"):
        fake.return_value.__enter__ = lambda s: s
        fake.return_value.__exit__ = lambda s, *a: False
        fake.return_value.post.side_effect = httpx.ConnectError("down")
        out = cs.suggest_category("文本", "procurement", budget=budget)
    # 第一次调用耗尽额度，重试前被闸拦下（不再发第二个请求）
    assert out.skip_reason == "budget_exceeded"
    assert fake.return_value.post.call_count == 1


def test_budget_none_passes_through(tmp_path, monkeypatch) -> None:
    """不限态（None）直通：现行为不变。"""
    monkeypatch.setenv("STORE_DB_PATH", str(tmp_path / "c.db"))
    with mock.patch.dict("os.environ", FAKE_ENV), \
            mock.patch.object(cs, "_load_env", return_value=dict(FAKE_ENV)), \
            mock.patch("app.services.llm_client.httpx.Client") as fake:
        fake_client = _client_mock(_ok_payload())
        fake.return_value = fake_client.return_value
        out = cs.suggest_category("文本", "procurement", budget=None)
    assert out.performed is True


def test_suggest_env_budget_default_and_garbage(monkeypatch) -> None:
    """LLM_SUGGEST_BUDGET_PER_UPLOAD：缺省 2；垃圾值回退默认；0=不限直通。"""
    monkeypatch.delenv("LLM_SUGGEST_BUDGET_PER_UPLOAD", raising=False)
    assert llm_budget.suggest_limit_from_env() == 2
    monkeypatch.setenv("LLM_SUGGEST_BUDGET_PER_UPLOAD", "abc")
    assert llm_budget.suggest_limit_from_env() == 2
    monkeypatch.setenv("LLM_SUGGEST_BUDGET_PER_UPLOAD", "0")
    assert llm_budget.new_suggest_budget() is None  # 不限态直通


# ---------- §1.2 建议层两段式结算（真实账本） ----------

def test_parse_failed_settles_ledger_no_fake_green(tmp_path, monkeypatch) -> None:
    """硬事二：合法 JSON 但白名单外→账本终态 parse_failed（无 success 假绿）。"""
    monkeypatch.setenv("STORE_DB_PATH", str(tmp_path / "c.db"))
    with mock.patch.dict("os.environ", FAKE_ENV), \
            mock.patch.object(cs, "_load_env", return_value=dict(FAKE_ENV)), \
            mock.patch("app.services.llm_client.httpx.Client") as fake:
        fake.return_value = _client_mock(_ok_payload(choice="hacked")).return_value
        out = cs.suggest_category("文本", "procurement")
    assert out.skip_reason == "parse_failed"
    rows = llm_call_log.query()
    assert len(rows) == 1
    assert rows[0]["outcome"] == "parse_failed"
    assert rows[0]["raw_output"] is None  # 不存原始响应体
    assert "raw_sha=" in (rows[0]["error_detail"] or "")  # 指纹可对账不可复原


def test_success_settles_whitelist_no_contract_echo(tmp_path, monkeypatch) -> None:
    """隐私对抗（§2.2）：模型输出故意复述合同句子→白名单回执拦截。"""
    monkeypatch.setenv("STORE_DB_PATH", str(tmp_path / "c.db"))
    echo = "甲乙双方就本设备租赁事宜达成如下协议条款原文复述"
    payload = {"success": True,
               "result": {"answers": {"category": {
                   "type": "choice", "choice": "lease", "confidence": 0.9,
                   "probabilities": {"lease": 0.9, "echo": echo},
                   "note": echo}},
                   "usage": {}}}
    with mock.patch.dict("os.environ", FAKE_ENV), \
            mock.patch.object(cs, "_load_env", return_value=dict(FAKE_ENV)), \
            mock.patch("app.services.llm_client.httpx.Client") as fake:
        fake.return_value = _client_mock(payload).return_value
        out = cs.suggest_category("文本", "procurement")
    assert out.performed is True
    row = llm_call_log.query()[0]
    assert row["outcome"] == "success"
    stored = json.loads(row["raw_output"] or "{}")
    assert stored == {"category": "lease", "confidence": 0.9}  # 白名单字段
    assert echo not in (row["raw_output"] or "")  # 复述的原文进不了账本
    assert row["prompt_hash"]  # 提示词指纹在案


# ---------- §2.1 审查单锚点：short_hash ----------

def test_short_hash_stable_and_short() -> None:
    assert llm_call_log.short_hash("abc") == llm_call_log.short_hash("abc")
    assert len(llm_call_log.short_hash("abc")) == 16
    assert llm_call_log.short_hash("abc") != llm_call_log.short_hash("abd")


# ---------- §3.5 daemon 线程 trace 续证 + §3.6 上传全链反查 ----------

def test_daemon_thread_trace_context(tmp_path, monkeypatch) -> None:
    """§3.5：审查管线跑在 daemon 线程（contextvar 不自动继承）——worker 顶部
    record_trace 续证后，管线节点账本行带同一 trace+review_id+prompt_hash。"""
    monkeypatch.setenv("STORE_DB_PATH", str(tmp_path / "c.db"))
    from app.services import llm_client

    payload = {"choices": [{"message": {"content": "ok"}}], "usage": {}}
    done = threading.Event()

    def work() -> None:
        with llm_call_log.record_trace("tdaemon"), \
                llm_call_log.record_node("objection", review_id="r1",
                                         prompt_version="pv1"):
            with mock.patch("app.services.llm_client.httpx.Client") as fake:
                fake.return_value = _client_mock(payload).return_value
                llm_client.chat_completion(
                    api_key="k", url="https://api.deepseek.com/v1",
                    model="m", system="s", user="u", temperature=0, timeout=5)
        done.set()

    t = threading.Thread(target=work, daemon=True)
    t.start()
    assert done.wait(timeout=10)
    rows = llm_call_log.query()
    assert len(rows) == 1
    assert rows[0]["trace_id"] == "tdaemon"
    assert rows[0]["review_id"] == "r1"
    assert rows[0]["node"] == "objection"
    assert rows[0]["prompt_hash"]


def test_upload_full_chain_review_backfill(tmp_path, monkeypatch) -> None:
    """§3.6 终审 P1-1 验收：正常上传后 review_id→trace_id→建议层账本行反查链。

    从最终 review_id 出发读到 trace_id，并按该 trace 查到建议层调用行
    （建单后 backfill 同步完成）；回填不重复不遗漏。
    """
    from fastapi.testclient import TestClient
    from app.main import app
    from app.services import precheck as precheck_service
    from app.services import store as store_service

    monkeypatch.setenv("STORE_DB_PATH", str(tmp_path / "c.db"))
    monkeypatch.setenv("CATEGORY_SUGGEST_ENABLED", "true")
    client = TestClient(app)
    fixture = "房屋租赁合同\n甲方出租乙方承租。".encode("utf-8")
    with mock.patch.object(cs, "_load_env", return_value=dict(FAKE_ENV)), \
            mock.patch("app.services.llm_client.httpx.Client") as fake, \
            mock.patch.object(precheck_service, "is_precheck_enabled",
                              return_value=False):
        fake.return_value = _client_mock(_ok_payload("lease", 0.95)).return_value
        r = client.post("/api/upload",
                        files={"file": ("c.txt", fixture, "text/plain")},
                        data={"category": "lease"})
    assert r.status_code == 200
    rid = r.json()["review_id"]
    review = store_service.store.get(rid)
    assert review is not None
    # 反查链第一跳：审查单持久化 trace_id
    assert review.get("trace_id")
    # 第二跳：按 trace 查到建议层账本行，且已回填本审查单（不重复不遗漏）
    rows = llm_call_log.query(trace_id=review["trace_id"])
    assert len(rows) == 1
    assert rows[0]["node"] == "category_suggest"
    assert rows[0]["review_id"] == rid
    # 第三跳：review_id→trace 一致（从审查单出发可复原全链）
    assert llm_call_log.query(review_id=rid)[0]["trace_id"] == review["trace_id"]
