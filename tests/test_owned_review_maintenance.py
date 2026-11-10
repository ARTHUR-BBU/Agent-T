import asyncio
import threading

import pytest

from app.services import owned_review_maintenance as maintenance
from app.services.owned_llm_ledger import OwnedLLMLedger
from app.services.owned_review_jobs import OwnedReviewJobs
from tests.test_owned_review_jobs import jobs  # noqa: F401


def test_recovery_revocation_preserves_live_jobs_and_marks_unknown(jobs, monkeypatch):  # noqa: F811
    accounts, owned, service, admin, alice_user, alice, bob = jobs
    ledger = OwnedLLMLedger(service)
    rid, job, secret = service.create(alice, filename="撤权任务", category="procurement")
    call = ledger.begin(alice, job, secret, node="quality", provider="zhipu", model="glm-5.2")
    live_rid, live_job, live_secret = service.create(bob, filename="有效任务", category="procurement")
    live_call = ledger.begin(bob, live_job, live_secret, node="quality", provider="zhipu", model="glm-5.2")
    accounts.set_active(admin, alice_user["id"], False)
    monkeypatch.setattr(maintenance, "_database_path", lambda: accounts.path)
    # Normal-mode ReviewStore performs legacy startup cleanup; account mode does not.
    monkeypatch.setattr(maintenance, "ReviewStore", lambda _path: owned.reviews)
    assert maintenance.recover_once() == (1, 1)
    assert maintenance.recover_once() == (0, 0)
    accounts.set_active(admin, alice_user["id"], True)
    new_alice = accounts.login("alice", "alice-password-123")
    assert owned.get(new_alice, rid)["status"] == "error"
    assert owned.get(bob, live_rid)["status"] == "processing"
    with accounts._connect() as conn:
        assert conn.execute("SELECT outcome FROM owned_llm_attempts WHERE id=?", (call,)).fetchone()[0] == "unknown"
        assert conn.execute("SELECT outcome FROM owned_llm_attempts WHERE id=?", (live_call,)).fetchone()[0] == "pending"


def test_stop_waits_for_running_maintenance(monkeypatch):
    entered, release = threading.Event(), threading.Event()

    def blocked():
        entered.set()
        assert release.wait(5)
        return 0, 0

    monkeypatch.setattr(maintenance, "recover_once", blocked)

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(maintenance.run(stop))
        assert await asyncio.to_thread(entered.wait, 5)
        stop.set()
        await asyncio.sleep(0.05)
        assert not task.done()
        release.set()
        await asyncio.wait_for(task, 5)

    asyncio.run(scenario())


def test_recovery_without_ledger_does_not_create_one(jobs, monkeypatch):  # noqa: F811
    accounts, owned, service, admin, alice_user, alice, _ = jobs
    rid, _, _ = service.create(alice, filename="旧任务", category="procurement")
    accounts.set_active(admin, alice_user["id"], False)
    monkeypatch.setattr(maintenance, "_database_path", lambda: accounts.path)
    monkeypatch.setattr(maintenance, "ReviewStore", lambda _path: owned.reviews)
    assert maintenance.recover_once() == (0, 1)
    with accounts._connect() as conn:
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='owned_llm_attempts'").fetchone() is None
    accounts.set_active(admin, alice_user["id"], True)
    assert owned.get(accounts.login("alice", "alice-password-123"), rid)["status"] == "error"


def test_missing_database_is_not_created(tmp_path, monkeypatch):
    path = tmp_path / "missing.sqlite"
    monkeypatch.setattr(maintenance, "_database_path", lambda: path)
    import pytest
    with pytest.raises(RuntimeError):
        maintenance.recover_once()
    assert not path.exists()


# ---- 2026-11-10 坏记录隔离批（docs/handoff-20261110-bad-record-isolation-before.md） ----

def test_poisoned_record_isolated_and_healthy_reaped(jobs, monkeypatch):  # noqa: F811
    """损坏 JSON 记录：任务行隔离、损坏审查行字节原样、健康任务同批推进。"""
    accounts, owned, service, admin, alice_user, alice, bob = jobs
    bad_rid, bad_job, _ = service.create(alice, filename="损坏记录", category="procurement")
    ok_rid, ok_job, _ = service.create(bob, filename="健康记录", category="procurement")
    with accounts._connect() as conn:
        conn.execute("UPDATE reviews SET data='{corrupted json!' WHERE id=?", (bad_rid,))
        bad_before = conn.execute("SELECT data FROM reviews WHERE id=?", (bad_rid,)).fetchone()[0]
    accounts.set_active(admin, alice_user["id"], False)
    accounts.set_active(admin, [u for u in accounts.list_users(admin) if u["username"] == "bobby"][0]["id"], False)
    monkeypatch.setattr(maintenance, "_database_path", lambda: accounts.path)
    monkeypatch.setattr(maintenance, "ReviewStore", lambda _p: owned.reviews)

    assert maintenance.recover_once() == (0, 1), "坏记录隔离后健康记录仍应推进"
    with accounts._connect() as conn:
        assert conn.execute("SELECT status FROM owned_review_jobs WHERE id=?", (bad_job,)).fetchone()[0] == "failed"
        assert conn.execute("SELECT status FROM owned_review_jobs WHERE id=?", (ok_job,)).fetchone()[0] == "failed"
        # 损坏审查行原样未动（绝不猜测改写——离线迁移的事）
        assert conn.execute("SELECT data FROM reviews WHERE id=?", (bad_rid,)).fetchone()[0] == bad_before
        assert conn.execute("SELECT status FROM owned_review_jobs WHERE status='running'").fetchone() is None
    # 第二轮：干净返回，无饥饿
    assert maintenance.recover_once() == (0, 0)


def test_status_mismatch_record_gets_honest_error(jobs, monkeypatch):  # noqa: F811
    """可解析但状态不符（status=done 而 job=running）：审查如实写 error、任务隔离。"""
    accounts, owned, service, admin, alice_user, alice, _ = jobs
    rid, job, _ = service.create(alice, filename="状态漂移", category="procurement")
    with accounts._connect() as conn:
        data = conn.execute("SELECT data FROM reviews WHERE id=?", (rid,)).fetchone()[0]
        import json as _json
        drifted = _json.loads(data)
        drifted["status"] = "done"  # 人为漂移：任务还在 running
        conn.execute("UPDATE reviews SET data=? WHERE id=?",
                     (_json.dumps(drifted, ensure_ascii=False), rid))
    accounts.set_active(admin, alice_user["id"], False)
    monkeypatch.setattr(maintenance, "_database_path", lambda: accounts.path)
    monkeypatch.setattr(maintenance, "ReviewStore", lambda _p: owned.reviews)
    # 可解析但状态不符 → 隔离（不写 error：本实现把不可控记录交给离线迁移）
    assert maintenance.recover_once() == (0, 0)
    with accounts._connect() as conn:
        assert conn.execute("SELECT status FROM owned_review_jobs WHERE id=?", (job,)).fetchone()[0] == "failed"
        # 审查 data 未被半改写（保存点回滚——状态不确定的内容不猜测）
        assert _json.loads(conn.execute("SELECT data FROM reviews WHERE id=?", (rid,)).fetchone()[0])["status"] == "done"
    assert maintenance.recover_once() == (0, 0)


def test_missing_review_row_quarantines_job(jobs, monkeypatch):  # noqa: F811
    """审查行缺失的任务行（旧库/历史损伤）：任务隔离，无异常外抛。"""
    accounts, owned, service, admin, alice_user, alice, _ = jobs
    rid, job, _ = service.create(alice, filename="孤儿任务", category="procurement")
    accounts.set_active(admin, alice_user["id"], False)
    # 模拟历史损伤：绕过 FK 直接删审查行
    with accounts._connect() as conn:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("DELETE FROM reviews WHERE id=?", (rid,))
    monkeypatch.setattr(maintenance, "_database_path", lambda: accounts.path)
    monkeypatch.setattr(maintenance, "ReviewStore", lambda _p: owned.reviews)
    assert maintenance.recover_once() == (0, 0)
    with accounts._connect() as conn:
        # codex P1-3：孤儿任务行已删除（消除数据级 FK 违例，上传路径随之恢复）
        assert conn.execute("SELECT 1 FROM owned_review_jobs WHERE id=?", (job,)).fetchone() is None
    assert maintenance.recover_once() == (0, 0)


def test_orphan_job_deletion_restores_upload_path(jobs, monkeypatch):  # noqa: F811
    """codex P1-3 验收：隔离孤儿后，严格检查的常规上传入口恢复可用。"""
    accounts, owned, service, admin, alice_user, alice, _ = jobs
    rid, job, _ = service.create(alice, filename="孤儿挡门", category="procurement")
    accounts.set_active(admin, alice_user["id"], False)
    with accounts._connect() as conn:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("DELETE FROM reviews WHERE id=?", (rid,))
    monkeypatch.setattr(maintenance, "_database_path", lambda: accounts.path)
    monkeypatch.setattr(maintenance, "ReviewStore", lambda _p: owned.reviews)
    assert maintenance.recover_once() == (0, 0)
    # 恢复后：严格构造检查（默认参数）不再被孤儿行拒绝
    strict = OwnedReviewJobs(owned)
    accounts.set_active(admin, alice_user["id"], True)
    alice_new = accounts.login("alice", "alice-password-123")
    new_rid, new_job, secret = strict.create(
        alice_new, filename="恢复后的新上传", category="procurement")
    assert strict.commit(alice_new, new_job, secret, result={"items": []}) == new_rid


def test_orphan_ledger_rows_do_not_block_real_recovery_entry(jobs, monkeypatch):  # noqa: F811
    """codex P1-1 集成验收：先造孤儿账本行，再走 recover_once 真实入口——
    构造器数据级检查放宽后孤儿收口可执行，健康恢复同批推进。"""
    accounts, owned, service, admin, alice_user, alice, bob = jobs
    ledger = OwnedLLMLedger(service)
    rid, job, secret = service.create(alice, filename="孤儿账本", category="procurement")
    call = ledger.begin(alice, job, secret, node="quality", provider="zhipu", model="glm-5.2")
    live_rid, live_job, _ = service.create(bob, filename="健康任务", category="procurement")
    # 先造孤儿：任务行+审查行消失（账本行还在）——此时才首次用恢复入口
    with accounts._connect() as conn:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("DELETE FROM owned_llm_attempts WHERE id=?", (call,))
        conn.execute("INSERT INTO owned_llm_attempts (id,job_id,node,provider,model,outcome,started_at) "
                     "VALUES ('orphan-ledger',?, 'quality','zhipu','glm-5.2','pending',1)", (job,))
        conn.execute("DELETE FROM owned_review_jobs WHERE id=?", (job,))
        conn.execute("DELETE FROM reviews WHERE id=?", (rid,))
    accounts.set_active(admin, alice_user["id"], False)
    accounts.set_active(admin, [u for u in accounts.list_users(admin) if u["username"] == "bobby"][0]["id"], False)
    monkeypatch.setattr(maintenance, "_database_path", lambda: accounts.path)
    monkeypatch.setattr(maintenance, "ReviewStore", lambda _p: owned.reviews)
    # 真实入口：孤儿不再挡门（ledger 构造放宽），孤儿收口 + 健康清理一次完成
    assert maintenance.recover_once() == (1, 1)
    with accounts._connect() as conn:
        assert conn.execute("SELECT outcome FROM owned_llm_attempts WHERE id='orphan-ledger'").fetchone()[0] == "unknown"
        assert conn.execute("SELECT status FROM owned_review_jobs WHERE id=?", (live_job,)).fetchone()[0] == "failed"
    assert maintenance.recover_once() == (0, 0)


def test_ownership_mismatch_pending_marked_unknown(jobs):  # noqa: F811
    """codex P1-2：审查行存在但归属不一致——两侧健康查询都选不到的
    pending 调用按 unknown 收口；绝不改写（可能属于其他账户的）审查内容。"""
    accounts, owned, service, admin, alice_user, alice, _ = jobs
    from app.services.owned_llm_ledger import OwnedLLMLedger
    ledger = OwnedLLMLedger(service)
    rid, job, secret = service.create(alice, filename="归属漂移", category="procurement")
    call = ledger.begin(alice, job, secret, node="quality", provider="zhipu", model="glm-5.2")
    # 模拟归属漂移：审查行 owner 换成 bob 的 user id（绕正常路径直接改库）
    bobby_user = [u for u in accounts.list_users(admin) if u["username"] == "bobby"][0]
    with accounts._connect() as conn:
        conn.execute("UPDATE reviews SET owner_user_id=? WHERE id=?", (bobby_user["id"], rid))
        before = conn.execute("SELECT data FROM reviews WHERE id=?", (rid,)).fetchone()[0]
    # alice 撤权让它进入恢复窗口
    accounts.set_active(admin, alice_user["id"], False)
    assert ledger.reconcile() == 1
    with accounts._connect() as conn:
        assert conn.execute("SELECT outcome FROM owned_llm_attempts WHERE id=?", (call,)).fetchone()[0] == "unknown"
        # 审查内容原样（不修改属于其他账户的审查）
        assert conn.execute("SELECT data FROM reviews WHERE id=?", (rid,)).fetchone()[0] == before


def test_transient_failure_is_not_misjudged_as_poison(jobs, monkeypatch):  # noqa: F811
    """codex P2：清理钩子暂时故障 → 本条回滚整批上抛，任务不被误隔离；
    故障解除后同一条正常恢复。"""
    accounts, owned, service, admin, alice_user, alice, _ = jobs
    rid, job, _ = service.create(alice, filename="暂时故障", category="procurement")
    accounts.set_active(admin, alice_user["id"], False)
    # 走进过期清理分支：审查行造老 + TTL 调小（否则 _remove_expired 不可达）
    owned.reviews._ttl = 100
    import time as _time
    with accounts._connect() as conn:
        conn.execute("UPDATE reviews SET created_at=? WHERE id=?", (_time.time() - 200, rid))
    import sqlite3 as _sqlite3
    from app.services.owned_review_store import OwnedReviewStore as _Store
    original = _Store._remove_expired

    def flaky(self, conn, review_id):
        raise _sqlite3.OperationalError(" injected transient failure")

    monkeypatch.setattr(_Store, "_remove_expired", flaky)
    with pytest.raises(_sqlite3.OperationalError):
        service.reap_stale()
    with accounts._connect() as conn:
        # 未被误判：任务仍 running（可重试），审查原样
        assert conn.execute("SELECT status FROM owned_review_jobs WHERE id=?", (job,)).fetchone()[0] == "running"
    # 故障解除 → 正常恢复（过期清理路径：审查连带任务行一并清除）
    monkeypatch.setattr(_Store, "_remove_expired", original)
    assert service.reap_stale() == 1
    with accounts._connect() as conn:
        assert conn.execute("SELECT 1 FROM owned_review_jobs WHERE id=?", (job,)).fetchone() is None
        assert conn.execute("SELECT 1 FROM reviews WHERE id=?", (rid,)).fetchone() is None


def test_poison_window_does_not_starve_healthy_behind(jobs):  # noqa: F811
    """公平推进：窗口内全是毒记录时，隔离后下轮窗口自然推进到健康记录。"""
    accounts, owned, service, admin, alice_user, alice, bob = jobs
    poison_ids = []
    for i in range(2):
        rid, job, _ = service.create(alice, filename=f"毒记录{i}", category="procurement")
        with accounts._connect() as conn:
            conn.execute("UPDATE reviews SET data='{broken!' WHERE id=?", (rid,))
        poison_ids.append(job)
    ok_rid, ok_job, _ = service.create(bob, filename="排后面的健康", category="procurement")
    accounts.set_active(admin, alice_user["id"], False)
    accounts.set_active(admin, [u for u in accounts.list_users(admin) if u["username"] == "bobby"][0]["id"], False)
    # 第一轮窗口 2：两条毒各被隔离；健康记录未入窗
    assert service.reap_stale(limit=2) == 0
    with accounts._connect() as conn:
        statuses = [r[0] for r in conn.execute(
            "SELECT status FROM owned_review_jobs WHERE id=?", (poison_ids[0],)).fetchall()]
    assert statuses == ["failed"]
    # 第二轮：毒已不占窗，健康记录被推进
    assert service.reap_stale(limit=2) == 1


def test_ledger_orphan_pending_marked_unknown(jobs):  # noqa: F811
    """账本孤儿 pending（任务/审查行已消失）：同 unknown 语义收口，不留永久 pending。"""
    accounts, owned, service, admin, alice_user, alice, _ = jobs
    from app.services.owned_llm_ledger import OwnedLLMLedger
    ledger = OwnedLLMLedger(service)
    rid, job, secret = service.create(alice, filename="孤儿调用", category="procurement")
    call = ledger.begin(alice, job, secret, node="quality", provider="zhipu", model="glm-5.2")
    # 模拟行消失（绕 FK）
    with accounts._connect() as conn:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("DELETE FROM owned_llm_attempts WHERE id=?", (call,))
        conn.execute("INSERT INTO owned_llm_attempts (id,job_id,node,provider,model,outcome,started_at) "
                     "VALUES ('orphan-x',?, 'quality','zhipu','glm-5.2','pending',1)", (job,))
        conn.execute("DELETE FROM owned_llm_attempts WHERE id=?", (call,))
        conn.execute("DELETE FROM owned_review_jobs WHERE id=?", (job,))
        conn.execute("DELETE FROM reviews WHERE id=?", (rid,))
    assert ledger.reconcile() == 1
    with accounts._connect() as conn:
        outcome = conn.execute("SELECT outcome FROM owned_llm_attempts WHERE id='orphan-x'").fetchone()[0]
    assert outcome == "unknown"
