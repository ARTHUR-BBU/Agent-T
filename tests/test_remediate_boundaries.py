"""补写入口不得将未读合同或主体事实当作可安全补写的内容。"""
import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext

import pytest
from fastapi import HTTPException
from app.api import routes
from app.api.schemas import AskRequest, RemediateEditRequest
from app.services import llm_ask, llm_remediate, remediation_budget, report, store as store_module


def test_long_contract_is_rejected_before_provider_call(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("长合同不得调用模型")

    monkeypatch.setattr(llm_ask, "_deepseek_key", forbidden)
    result = llm_remediate.remediate_item(
        item={"id": "payment"}, contract_text="甲" * (llm_remediate.MAX_CONTRACT_CHARS + 1))
    assert result["ok"] is False
    assert "长度上限" in result["error"]
    empty = llm_remediate.remediate_item(item={"id": "payment"}, contract_text=" ")
    assert empty["mode"] == "error" and "正文不可用" in empty["error"]


def test_subject_identity_never_reaches_model(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("主体信息不得调用模型")

    monkeypatch.setattr(llm_ask, "_deepseek_key", forbidden)
    for item_id in ("subject", "lessor_title", "signature", "unfair_terms", "unknown_new_item"):
        result = llm_remediate.remediate_item(item={"id": item_id}, contract_text="合同")
        assert result["mode"] == "guidance"
    # 采购标的不是签约主体信息。
    assert not llm_remediate.is_negative_item({"id": "subject_matter"})


def test_model_result_rejects_wrong_types_and_preserves_refusal():
    malformed = {"elsewhere": {"found": "false"},
                 "draft": {"needed": True, "clause_draft": "草稿"}}
    assert not llm_remediate._parse_result(json.dumps(malformed))["ok"]
    malformed["elsewhere"] = {"found": False, "quotes": 42}
    assert not llm_remediate._parse_result(json.dumps(malformed))["ok"]
    malformed["elsewhere"] = {"found": False, "quotes": []}
    malformed["draft"]["clause_draft"] = "甲" * 6001
    assert not llm_remediate._parse_result(json.dumps(malformed))["ok"]
    refusal = {"elsewhere": {"found": False}, "draft": {"needed": False},
               "refuse_reason": "请咨询专业人士"}
    parsed = llm_remediate._parse_result(json.dumps(refusal))
    assert llm_remediate._post_process(parsed, "合同")["error"] == "请咨询专业人士"


def test_protected_context_and_unverified_values_fail_closed(monkeypatch):
    monkeypatch.setattr(llm_ask, "_deepseek_key", lambda: (_ for _ in ()).throw(
        AssertionError("特殊保护领域不得调用模型")))
    result = llm_remediate.remediate_item(
        item={"id": "breach"}, contract_text="本劳动合同涉及劳动者权益")
    assert result["mode"] == "guidance"
    draft = {"ok": True, "elsewhere": {"found": False, "quotes": []},
             "draft": {"needed": True, "clause_draft": "三十天内付款",
                       "related": [], "blanks": []}}
    assert llm_remediate._post_process(draft, "合同")["mode"] == "error"
    draft["draft"]["clause_draft"] = "违约金按价款的百分之十计算"
    assert llm_remediate._post_process(draft, "合同")["mode"] == "error"
    draft["draft"]["clause_draft"] = "向北京市朝阳区人民法院起诉"
    assert llm_remediate._post_process(draft, "合同")["mode"] == "error"
    draft["draft"]["clause_draft"] = "期限为____（待填写：期限）"
    draft["draft"]["related"] = ["可参照第十条", "可参照第二条"]
    assert llm_remediate._post_process(draft, "第二条 付款")["draft"]["related"] == ["可参照第二条"]


def test_model_cannot_turn_a_quote_into_a_legal_conclusion():
    result = {"ok": True, "elsewhere": {"found": True,
              "summary": "该合同条款无效，应当起诉", "quotes": ["双方书面通知"]},
              "draft": {"needed": False, "clause_draft": "", "related": [], "blanks": []}}
    got = llm_remediate._post_process(result, "双方书面通知")
    assert got["mode"] == "elsewhere" and got["quote_verified"]
    assert "条款无效" not in got["elsewhere"]["summary"]


def test_budget_is_atomic_persistent_and_purged(tmp_path, monkeypatch):
    isolated = store_module.ReviewStore(str(tmp_path / "reviews.sqlite"), ttl_seconds=1)
    monkeypatch.setattr(store_module, "store", isolated)
    rid = isolated.create(status="done")
    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(lambda _: remediation_budget.reserve(rid, "payment"), range(8)))
    assert outcomes.count("reserved") == 1
    assert outcomes.count("processing") == 7
    remediation_budget.finish(rid, "payment", success=True)
    assert remediation_budget.reserve(rid, "payment") == "success"
    for i in range(remediation_budget.MAX_ATTEMPTS_PER_REVIEW - 1):
        assert remediation_budget.reserve(rid, str(i)) == "reserved"
        remediation_budget.finish(rid, str(i), success=False)
    assert remediation_budget.reserve(rid, "another") == "exhausted"
    # 随审查单实际删除的事务一起清理，不留下合同关联记录。
    with isolated._conn() as conn:
        conn.execute("UPDATE reviews SET created_at=0 WHERE id=?", (rid,))
    assert isolated.get(rid) is None
    assert remediation_budget.reserve(rid, "another") == "expired"
    with isolated._conn() as conn:
        assert conn.execute("SELECT count(*) FROM remediation_calls").fetchone()[0] == 0


def test_segmented_review_checks_middle_and_refuses_incomplete_scan(monkeypatch):
    monkeypatch.setattr(llm_ask, "_deepseek_key", lambda: "test-key")
    text = "甲" * 6200 + "验收后支付全部货款" + "乙" * 6300
    calls = []

    def chat(key, system, user):
        calls.append(user)
        found = "验收后支付全部货款" in user
        return json.dumps({"found": found, "summary": "可能有关",
                           "quotes": ["验收后支付全部货款"] if found else []}, ensure_ascii=False)

    monkeypatch.setattr(llm_ask, "_chat_deepseek", chat)
    result = llm_remediate.remediate_item(item={"id": "payment", "name": "付款"}, contract_text=text)
    assert result["mode"] == "elsewhere" and result["quote_verified"]
    assert len(calls) == 2
    monkeypatch.setattr(llm_ask, "_chat_deepseek", lambda *a: "invalid output")
    assert llm_remediate.remediate_item(item={"id": "payment"}, contract_text=text)["mode"] == "error"
    monkeypatch.setattr(llm_ask, "_chat_deepseek", lambda *a: json.dumps(
        {"found": False, "quotes": ["疑似相关原文"]}, ensure_ascii=False))
    assert llm_remediate.remediate_item(item={"id": "payment"}, contract_text=text)["mode"] == "error"
    assert llm_remediate.calls_needed("甲" * 200_000) <= remediation_budget.MAX_CALLS_PER_REVIEW


def test_segmented_draft_only_after_every_chunk_was_checked(monkeypatch):
    monkeypatch.setattr(llm_ask, "_deepseek_key", lambda: "test-key")
    text = "甲" * 6000 + "中间哨兵" + "乙" * 6000 + "结尾哨兵"
    seen = []

    def chat(key, system, user):
        seen.append((system, user))
        if "合同片段是否" in system:
            return json.dumps({"found": False, "summary": "", "quotes": []})
        return json.dumps({"elsewhere": {"found": False, "quotes": []},
                           "draft": {"needed": True, "clause_draft": "期限____（待填写：期限）"}},
                          ensure_ascii=False)

    monkeypatch.setattr(llm_ask, "_chat_deepseek", chat)
    result = llm_remediate.remediate_item(item={"id": "payment"}, contract_text=text)
    assert result["mode"] == "draft"
    assert len(seen) == llm_remediate.calls_needed(text)
    assert any("中间哨兵" in user for _, user in seen[:-1])
    assert "结尾哨兵" in seen[-2][1]
    assert "首尾摘要" in seen[-1][1]


def test_large_reservation_cannot_exceed_remaining_call_budget(tmp_path, monkeypatch):
    isolated = store_module.ReviewStore(str(tmp_path / "budget.sqlite"), ttl_seconds=60)
    monkeypatch.setattr(store_module, "store", isolated)
    rid = isolated.create(status="done")
    assert remediation_budget.reserve(rid, "a", calls=35) == "reserved"
    remediation_budget.finish(rid, "a", success=False)
    assert remediation_budget.reserve(rid, "b", calls=2) == "exhausted"
    assert remediation_budget.reserve(rid, "b", calls=1) == "reserved"


def test_interrupted_process_recovers_without_automatic_model_retry(tmp_path, monkeypatch):
    path = str(tmp_path / "restart.sqlite")
    monkeypatch.setenv("STORE_DB_PATH", path)
    isolated = store_module.ReviewStore(path, ttl_seconds=60)
    monkeypatch.setattr(store_module, "store", isolated)
    rid = isolated.create(status="done")
    script = r'''
import os, sys
from app.services import remediation_budget as budget
with budget.item_lock(sys.argv[1], "payment") as guard:
    assert guard is not None
    assert budget.reserve(sys.argv[1], "payment", calls=3, guard=guard) == "reserved"
    budget.note_call_started(guard)
    os._exit(0)
'''
    env = os.environ.copy()
    env["STORE_DB_PATH"] = path
    result = subprocess.run([sys.executable, "-c", script, rid], env=env,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    with remediation_budget.item_lock(rid, "payment") as guard:
        assert guard is not None
        # 首次访问只标记中断，不发起新的模型请求。
        assert remediation_budget.reserve(rid, "payment", calls=3, guard=guard) == "interrupted"
    with isolated._conn() as conn:
        attempts, reserved, state = conn.execute(
            "SELECT attempts, reserved_calls, state FROM remediation_calls WHERE review_id=?", (rid,)
        ).fetchone()
    assert (attempts, reserved, state) == (1, 1, "interrupted")
    with remediation_budget.item_lock(rid, "payment") as guard:
        assert guard is not None
        assert remediation_budget.reserve(rid, "payment", calls=3, guard=guard) == "reserved"
        remediation_budget.note_call_started(guard)
        remediation_budget.finish(rid, "payment", success=False)


def test_active_lock_and_legacy_processing_fail_closed(tmp_path, monkeypatch):
    isolated = store_module.ReviewStore(str(tmp_path / "lock.sqlite"), ttl_seconds=60)
    monkeypatch.setattr(store_module, "store", isolated)
    rid = isolated.create(status="done")
    with remediation_budget.item_lock(rid, "payment") as first:
        assert first is not None
        with remediation_budget.item_lock(rid, "payment") as second:
            assert second is None
    assert remediation_budget.reserve(rid, "legacy", calls=1) == "reserved"
    with remediation_budget.item_lock(rid, "legacy") as guard:
        assert guard is not None
        assert remediation_budget.reserve(rid, "legacy", calls=1, guard=guard) == "legacy_processing"


def test_call_cannot_exceed_reserved_slots_or_use_released_guard(tmp_path, monkeypatch):
    isolated = store_module.ReviewStore(str(tmp_path / "limits.sqlite"), ttl_seconds=60)
    monkeypatch.setattr(store_module, "store", isolated)
    rid = isolated.create(status="done")
    with remediation_budget.item_lock(rid, "payment") as guard:
        assert guard is not None
        assert remediation_budget.reserve(rid, "payment", calls=1, guard=guard) == "reserved"
        remediation_budget.note_call_started(guard)
        with pytest.raises(RuntimeError, match="没有有效预留"):
            remediation_budget.note_call_started(guard)
    with pytest.raises(RuntimeError, match="锁凭证无效"):
        remediation_budget.note_call_started(guard)


@pytest.mark.parametrize("has_reserved", [False, True])
def test_old_budget_schema_migrates_without_assuming_old_worker_stopped(tmp_path, monkeypatch, has_reserved):
    isolated = store_module.ReviewStore(str(tmp_path / "old-schema.sqlite"), ttl_seconds=60)
    monkeypatch.setattr(store_module, "store", isolated)
    rid = isolated.create(status="done")
    with isolated._conn() as conn:
        extra = ", reserved_calls INTEGER NOT NULL DEFAULT 0" if has_reserved else ""
        conn.execute("CREATE TABLE remediation_calls (review_id TEXT, item_id TEXT, "
                     "attempts INTEGER NOT NULL, state TEXT NOT NULL" + extra +
                     ", PRIMARY KEY(review_id, item_id))")
        if has_reserved:
            conn.execute("INSERT INTO remediation_calls VALUES (?, ?, 2, 'processing', 5)", (rid, "payment"))
        else:
            conn.execute("INSERT INTO remediation_calls VALUES (?, ?, 2, 'processing')", (rid, "payment"))
    with remediation_budget.item_lock(rid, "payment") as guard:
        assert guard is not None
        assert remediation_budget.reserve(rid, "payment", guard=guard) == "legacy_processing"
    with isolated._conn() as conn:
        state, reserved = conn.execute(
            "SELECT state, reserved_calls FROM remediation_calls WHERE review_id=?", (rid,)
        ).fetchone()
    assert state == "processing" and reserved == (5 if has_reserved else 2)


def test_legacy_retry_after_new_worker_never_inherits_new_lock_claim(tmp_path, monkeypatch):
    isolated = store_module.ReviewStore(str(tmp_path / "mixed-version.sqlite"), ttl_seconds=60)
    monkeypatch.setattr(store_module, "store", isolated)
    rid = isolated.create(status="done")
    with remediation_budget.item_lock(rid, "payment") as guard:
        assert guard is not None
        assert remediation_budget.reserve(rid, "payment", guard=guard) == "reserved"
        remediation_budget.finish(rid, "payment", success=False)
    # 模拟旧进程用旧 UPSERT 开始重试：它不会更新新版 generation_token。
    with isolated._conn() as conn:
        conn.execute(
            "UPDATE remediation_calls SET attempts=attempts+1, reserved_calls=reserved_calls+1, "
            "state='processing' WHERE review_id=? AND item_id=?", (rid, "payment"))
    with remediation_budget.item_lock(rid, "payment") as guard:
        assert guard is not None
        assert remediation_budget.reserve(rid, "payment", guard=guard) == "legacy_processing"
    with isolated._conn() as conn:
        state, lock_version = conn.execute(
            "SELECT state, lock_version FROM remediation_calls WHERE review_id=?", (rid,)
        ).fetchone()
    assert (state, lock_version) == ("processing", 0)


def test_route_interruption_warns_before_retry_and_charges_started_call(tmp_path, monkeypatch):
    isolated = store_module.ReviewStore(str(tmp_path / "recovery-route.sqlite"), ttl_seconds=60)
    monkeypatch.setattr(store_module, "store", isolated)
    monkeypatch.setattr(routes, "store", isolated)
    monkeypatch.setattr(llm_ask, "_deepseek_key", lambda: "test-key")
    monkeypatch.setattr(routes.llm_call_log, "record_node", lambda *a, **kw: nullcontext())
    rid = isolated.create(status="done", text="合同", items=[
        {"id": "payment", "name": "付款", "status": "未找到"}])
    # 模拟前一个进程已发出请求后退出，操作系统释放锁。
    with remediation_budget.item_lock(rid, "payment") as old:
        assert old is not None
        assert remediation_budget.reserve(rid, "payment", guard=old) == "reserved"
        remediation_budget.note_call_started(old)
    model_calls = []

    def generate(**kwargs):
        kwargs["before_call"]()
        model_calls.append(1)
        return {"ok": True, "mode": "draft", "draft": {"clause_draft": "示范文本"}}

    monkeypatch.setattr(llm_remediate, "remediate_item", generate)
    with pytest.raises(HTTPException) as warning:
        routes.remediate_item(rid, "payment")
    assert warning.value.status_code == 409
    assert "可能已计费" in warning.value.detail
    assert model_calls == []
    assert routes.remediate_item(rid, "payment").mode == "draft"
    assert model_calls == [1]
    with isolated._conn() as conn:
        attempts, reserved = conn.execute(
            "SELECT attempts, reserved_calls FROM remediation_calls WHERE review_id=?", (rid,)
        ).fetchone()
    assert (attempts, reserved) == (2, 2)


def test_other_process_holds_lock_until_exit(tmp_path, monkeypatch):
    path = str(tmp_path / "concurrent-process.sqlite")
    monkeypatch.setenv("STORE_DB_PATH", path)
    isolated = store_module.ReviewStore(path, ttl_seconds=60)
    monkeypatch.setattr(store_module, "store", isolated)
    rid = isolated.create(status="done")
    ready_file = tmp_path / "lock-ready"
    script = r'''
import sys
from pathlib import Path
from app.services import remediation_budget as budget
with budget.item_lock(sys.argv[1], "payment") as guard:
    assert guard is not None
    assert budget.reserve(sys.argv[1], "payment", guard=guard) == "reserved"
    Path(sys.argv[2]).write_text("ready")
    sys.stdin.readline()
'''
    env = os.environ.copy()
    env["STORE_DB_PATH"] = path
    proc = subprocess.Popen([sys.executable, "-c", script, rid, str(ready_file)], env=env,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 20
        while not ready_file.exists() and proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert ready_file.exists(), "子进程未能在时限内取得锁"
        with remediation_budget.item_lock(rid, "payment") as other:
            assert other is None
    finally:
        if proc.stdin and proc.poll() is None:
            proc.stdin.write("exit\n")
            proc.stdin.flush()
        try:
            proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate(timeout=10)
    assert proc.returncode == 0


def test_route_counts_each_segment_before_provider_call(tmp_path, monkeypatch):
    isolated = store_module.ReviewStore(str(tmp_path / "segments.sqlite"), ttl_seconds=60)
    monkeypatch.setattr(store_module, "store", isolated)
    monkeypatch.setattr(routes, "store", isolated)
    monkeypatch.setattr(llm_ask, "_deepseek_key", lambda: "test-key")
    monkeypatch.setattr(routes.llm_call_log, "record_node", lambda *a, **kw: nullcontext())
    text = "甲" * 6000 + "中间" + "乙" * 6000
    rid = isolated.create(status="done", text=text, items=[
        {"id": "payment", "name": "付款", "status": "未找到"}])
    calls = []

    def chat(key, system, user):
        with isolated._conn() as conn:
            started = conn.execute(
                "SELECT started_calls FROM remediation_calls WHERE review_id=?", (rid,)
            ).fetchone()[0]
        calls.append(started)
        if "合同片段是否" in system:
            return json.dumps({"found": False, "summary": "", "quotes": []})
        return json.dumps({"elsewhere": {"found": False}, "draft": {
            "needed": True, "clause_draft": "期限____（待填写：期限）"}}, ensure_ascii=False)

    monkeypatch.setattr(llm_ask, "_chat_deepseek", chat)
    assert routes.remediate_item(rid, "payment").mode == "draft"
    assert calls == list(range(1, llm_remediate.calls_needed(text) + 1))


def test_concurrent_route_calls_do_not_duplicate_model_request(tmp_path, monkeypatch):
    isolated = store_module.ReviewStore(str(tmp_path / "parallel.sqlite"), ttl_seconds=60)
    monkeypatch.setattr(store_module, "store", isolated)
    monkeypatch.setattr(routes, "store", isolated)
    monkeypatch.setattr(llm_ask, "_deepseek_key", lambda: "test-key")
    monkeypatch.setattr(routes.llm_call_log, "record_node", lambda *a, **kw: nullcontext())
    rid = isolated.create(status="done", text="合同", items=[
        {"id": "payment", "name": "付款", "status": "未找到"}])
    entered, release = threading.Event(), threading.Event()
    calls = []

    def generate(**kwargs):
        kwargs["before_call"]()
        calls.append(1)
        entered.set()
        assert release.wait(10)
        return {"ok": True, "mode": "draft", "draft": {"clause_draft": "示范"}}

    monkeypatch.setattr(llm_remediate, "remediate_item", generate)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(routes.remediate_item, rid, "payment")
        try:
            assert entered.wait(10)
            with pytest.raises(HTTPException) as busy:
                routes.remediate_item(rid, "payment")
            assert busy.value.status_code == 409
        finally:
            release.set()
        assert first.result(timeout=10).mode == "draft"
    assert calls == [1]


def test_nested_updates_from_separate_store_instances_do_not_overwrite(tmp_path):
    path = str(tmp_path / "concurrent.sqlite")
    stores = [store_module.ReviewStore(path, ttl_seconds=60) for _ in range(4)]
    rid = stores[0].create(status="done", remediations={})

    def write(i):
        def change(row):
            row["remediations"][str(i)] = {"mode": "draft"}
        stores[i % len(stores)].mutate(rid, change)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(write, range(20)))
    assert len(stores[0].get(rid)["remediations"]) == 20


def test_route_reuses_saved_result_and_keeps_adoption(tmp_path, monkeypatch):
    isolated = store_module.ReviewStore(str(tmp_path / "routes.sqlite"), ttl_seconds=60)
    monkeypatch.setattr(store_module, "store", isolated)
    monkeypatch.setattr(routes, "store", isolated)
    monkeypatch.setattr(llm_ask, "_deepseek_key", lambda: "test-key")
    monkeypatch.setattr(routes.llm_call_log, "record_node", lambda *a, **kw: nullcontext())
    calls = []

    def generate(**kwargs):
        calls.append(kwargs)
        return {"ok": True, "mode": "draft", "draft": {"clause_draft": "待填写"}}

    monkeypatch.setattr(llm_remediate, "remediate_item", generate)
    rid = isolated.create(status="done", text="合同", items=[
        {"id": "payment", "name": "支付", "status": "未找到"}])
    assert routes.remediate_item(rid, "payment").mode == "draft"
    assert routes.adopt_remediation(rid, "payment").adopted
    assert routes.remediate_item(rid, "payment").mode == "draft"
    assert len(calls) == 1
    saved = isolated.get(rid)
    assert saved["remediations"]["payment"]["adopted"] is True
    assert saved["items"][0]["status"] == "未找到"
    result = routes.edit_remediation(rid, "payment", RemediateEditRequest(
        clause_draft="用户调整的文本", expected_draft="待填写"))
    assert result.user_edited and not result.adopted
    saved = isolated.get(rid)
    entry = saved["remediations"]["payment"]
    assert entry["initial_draft"]["clause_draft"] == "待填写"
    assert entry["past_adoptions"][0]["clause_draft"] == "待填写"
    assert entry["draft"]["clause_draft"] == "用户调整的文本"
    assert saved["items"][0]["status"] == "未找到"
    with pytest.raises(HTTPException) as stale:
        routes.edit_remediation(rid, "payment", RemediateEditRequest(
            clause_draft="过期页面的文本", expected_draft="待填写"))
    assert stale.value.status_code == 409
    assert isolated.get(rid)["remediations"]["payment"]["draft"]["clause_draft"] == "用户调整的文本"


def test_read_hides_legacy_draft_for_protected_item(tmp_path, monkeypatch):
    path = str(tmp_path / "legacy.sqlite")
    monkeypatch.setenv("STORE_DB_PATH", path)
    isolated = store_module.ReviewStore(path, ttl_seconds=60)
    monkeypatch.setattr(routes, "store", isolated)
    rid = isolated.create(status="done", filename="test.txt", category="procurement",
                          text="甲方乙方签署合同", items=[
                              {"id": "subject", "name": "主体", "status": "未找到"}],
                          remediations={"subject": {"mode": "draft", "draft": {
                              "clause_draft": "不应展示的历史草稿"}}})
    view = routes.get_review(rid)
    assert view.remediations["subject"]["mode"] == "guidance"
    assert "draft" not in view.remediations["subject"]
    assert isolated.get(rid)["remediations"]["subject"]["mode"] == "draft"


def test_remediation_context_only_returns_unique_verified_original(tmp_path, monkeypatch):
    path = str(tmp_path / "context.sqlite")
    monkeypatch.setenv("STORE_DB_PATH", path)
    isolated = store_module.ReviewStore(path, ttl_seconds=60)
    monkeypatch.setattr(routes, "store", isolated)
    text = "第一条 合同说明。第二条 验收后支付全部货款。第三条 争议解决。"
    quote = "验收后支付全部货款"
    rid = isolated.create(status="done", text=text, items=[
        {"id": "payment", "name": "付款", "status": "未找到"}],
        clause_index={"clauses": [{"id": "c2", "heading": "第二条",
                                   "start": text.index("第二条"), "end": text.index("第三条")}]},
        remediations={"payment": {"mode": "elsewhere", "elsewhere": {"quotes": [quote]}}})
    context = routes.get_remediation_context(rid, "payment", 0)
    assert context.status == "verified"
    assert context.heading == "第二条"
    assert context.before + context.match + context.after == text
    assert context.match == quote
    from tests.test_evidence_claims import client
    response = client.get(f"/api/review/{rid}/items/payment/remediate/context/0")
    assert response.status_code == 200 and response.json()["match"] == quote
    view = routes.get_review(rid)
    assert view.remediations["payment"]["elsewhere"]["context_available"] is True
    assert "context_available" not in isolated.get(rid)["remediations"]["payment"]["elsewhere"]
    isolated.update(rid, clause_index={"clauses": [{"id": "c2", "heading": "虚假的标题",
                                                     "start": 0, "end": len(text)}]})
    assert routes.get_remediation_context(rid, "payment", 0).heading == ""
    with pytest.raises(HTTPException) as missing:
        routes.get_remediation_context(rid, "payment", 1)
    assert missing.value.status_code == 404
    isolated.update(rid, text=text + quote)
    ambiguous = routes.get_remediation_context(rid, "payment", 0)
    assert ambiguous.status == "ambiguous"
    assert not (ambiguous.before or ambiguous.match or ambiguous.after)
    isolated.update(rid, text="该合同没有该摘句")
    assert routes.get_remediation_context(rid, "payment", 0).status == "missing"


def test_remediation_context_rejects_protected_legacy_draft(tmp_path, monkeypatch):
    isolated = store_module.ReviewStore(str(tmp_path / "protected-context.sqlite"), ttl_seconds=60)
    monkeypatch.setattr(routes, "store", isolated)
    rid = isolated.create(status="done", text="主体资格信息", items=[
        {"id": "subject", "name": "主体", "status": "未找到"}],
        remediations={"subject": {"mode": "elsewhere", "elsewhere": {"quotes": ["主体资格"]}}})
    with pytest.raises(HTTPException) as forbidden:
        routes.get_remediation_context(rid, "subject", 0)
    assert forbidden.value.status_code == 404


def test_remediation_context_limits_disclosed_text(tmp_path, monkeypatch):
    isolated = store_module.ReviewStore(str(tmp_path / "bounded-context.sqlite"), ttl_seconds=60)
    monkeypatch.setattr(routes, "store", isolated)
    quote = "起" + "甲" * 1500 + "止"
    text = "前" * 3000 + quote + "后" * 3000
    rid = isolated.create(status="done", text=text, items=[
        {"id": "payment", "name": "付款", "status": "未找到"}],
        remediations={"payment": {"mode": "elsewhere", "elsewhere": {"quotes": [quote]}}})
    result = routes.get_remediation_context(rid, "payment", 0)
    assert result.status == "verified" and result.truncated
    assert len(result.before) == 500 and len(result.match) == 1200
    assert result.after == ""
    assert result.match == quote[:1200]


def test_unanchored_opinion_is_explain_only_and_labeled(tmp_path, monkeypatch):
    isolated = store_module.ReviewStore(str(tmp_path / "opinions.sqlite"), ttl_seconds=60)
    monkeypatch.setattr(routes, "store", isolated)
    monkeypatch.setattr(routes.llm_call_log, "record_node", lambda *a, **kw: nullcontext())
    monkeypatch.setattr(routes, "_record_ask_evidence", lambda **kw: None)
    monkeypatch.setattr(routes.ai_findings_service, "review_findings", lambda row: {"findings": [
        {"finding_key": "obs:impact:123", "fact_type": False, "status": "viewed",
         "title": "可能存在遗漏", "comment": "需要进一步核对"}]})
    observed = []
    monkeypatch.setattr(llm_ask, "ask_about_item", lambda **kw: (
        observed.append(kw) or {"ok": True, "answer": {"问题是啥": "需要核对"}}))
    rid = isolated.create(status="done", text="合同", items=[])
    response = routes.ask(AskRequest(review_id=rid, item_id="opinion:obs:impact:123",
                                     question="这是什么意思", request_id="o1"))
    assert response.ok and observed[0]["item"]["tag_source"] == "ai_opinion"
    saved = isolated.get(rid)
    assert saved["items"] == []
    assert saved["ask_thread"]["turns"][0]["source"] == "opinion"


def test_opinion_model_output_cannot_publish_rewrite_or_raw(monkeypatch):
    monkeypatch.setattr(llm_ask, "_deepseek_key", lambda: "test-key")
    monkeypatch.setattr(llm_ask, "_chat_deepseek", lambda *a: json.dumps({
        "问题是啥": "该合同条款无效，应当起诉", "原文在哪": "合同原文", "改写稿": "不应展示的草稿",
        "建议怎么改": "不应展示的建议"}, ensure_ascii=False))
    result = llm_ask.ask_about_item(question="说明", item={
        "id": "opinion:x", "status": "AI 观点", "tag_source": "ai_opinion"},
        contract_text="合同原文", policies=[])
    assert result["answer"]["改写稿"] == ""
    assert "不应展示的建议" not in result["answer"]["建议怎么改"]
    assert "合同条款无效" not in result["answer"]["问题是啥"]
    assert "不构成法律意见" in result["answer"]["问题是啥"]
    assert result["raw_text"] == ""


def test_report_keeps_draft_adoption_and_discussion_origins():
    from docx import Document
    doc = Document()
    report._remediations(doc, {"remediation_records": [{
        "item_name": "付款", "mode": "draft", "adopted": True,
        "draft": {"clause_draft": "期限____（待填写：期限）"}}]})
    report._ask_thread(doc, {"ask_thread_section": [
        {"item_name": "观点甲", "source": "opinion", "question": "为什么", "state": "success",
         "answer": {"问题是啥": "请核对"}},
        {"item_name": "付款", "source": "remediate", "question": "如何理解", "state": "success",
         "answer": {"问题是啥": "这是示范"}}]})
    text = "\n".join(p.text for p in doc.paragraphs)
    assert "期限____（待填写：期限）" in text
    assert "用户已选用（系统未验证）" in text
    assert "关于：观点甲（AI 观点）" in text
    assert "关于：付款 · 补写" in text


def test_report_distinguishes_user_edit_and_previous_adoption():
    from docx import Document
    doc = Document()
    report._remediations(doc, {"remediation_records": [{
        "item_name": "付款", "mode": "draft", "adopted": False, "user_edited": True,
        "initial_draft": {"clause_draft": "AI 最初文本"},
        "past_adoptions": [{"clause_draft": "用户此前选用的文本"}],
        "draft": {"clause_draft": "用户当前文本"}}]})
    text = "\n".join(p.text for p in doc.paragraphs)
    for expected in ("AI 最初示范文本", "AI 最初文本", "此前选用文本", "用户此前选用的文本",
                     "用户调整后的文本", "用户当前文本", "选用状态：未选用"):
        assert expected in text
