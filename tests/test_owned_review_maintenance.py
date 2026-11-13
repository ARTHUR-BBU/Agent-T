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


# ---- codex 二轮审核（2026-11-12）：四条 P1 + 公平性 ----

def test_orphan_job_with_ledger_evidence_is_kept_not_deleted(jobs, monkeypatch):  # noqa: F811
    """codex 二轮 P1-1：孤儿任务带账本行（pending/unknown/已结算混合）→
    任务行保留待离线迁移，绝不级联删除任何调用证据。"""
    accounts, owned, service, admin, alice_user, alice, _ = jobs
    from app.services.owned_llm_ledger import OwnedLLMLedger
    ledger = OwnedLLMLedger(service)
    rid, job, secret = service.create(alice, filename="带证据孤儿", category="procurement")
    call_pending = ledger.begin(alice, job, secret, node="quality", provider="zhipu", model="glm-5.2")
    call_settled = ledger.begin(alice, job, secret, node="ask", provider="zhipu", model="glm-5.2")
    ledger.settle(alice, job, secret, call_settled, outcome="success", input_tokens=10, output_tokens=5)
    accounts.set_active(admin, alice_user["id"], False)
    # 制造孤儿：审查行消失（任务行+3 条账本行保留）
    with accounts._connect() as conn:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("DELETE FROM reviews WHERE id=?", (rid,))
    monkeypatch.setattr(maintenance, "_database_path", lambda: accounts.path)
    monkeypatch.setattr(maintenance, "ReviewStore", lambda _p: owned.reviews)
    maintenance.recover_once()
    with accounts._connect() as conn:
        # 任务行保留（failed），账本证据一条不少；reconcile 先行把孤儿
        # pending 收口为 unknown（时序：reconcile → reap_stale）
        assert conn.execute("SELECT status FROM owned_review_jobs WHERE id=?", (job,)).fetchone()[0] == "failed"
        rows = conn.execute("SELECT id,outcome FROM owned_llm_attempts WHERE job_id=?", (job,)).fetchall()
        assert len(rows) == 2
        outcomes = {r["id"]: r["outcome"] for r in rows}
        assert outcomes[call_pending] == "unknown", "孤儿 pending 已被 reconcile 收口"
        assert outcomes[call_settled] == "success", "已结算证据原样"
    # 后续轮次幂等：不再产生变化
    assert maintenance.recover_once() == (0, 0)
    with accounts._connect() as conn:
        rows = conn.execute("SELECT outcome FROM owned_llm_attempts WHERE job_id=?", (job,)).fetchall()
        assert sorted(r[0] for r in rows) == ["success", "unknown"], "证据仍一条不少"


def test_ownership_mismatch_job_quarantined_without_deletion(jobs):  # noqa: F811
    """codex 二轮 P1-1：归属不匹配（审查存在但属他人）→ 置 failed 隔离，
    不删任务行（无 FK 违例）且绝不触碰他人审查内容。"""
    accounts, owned, service, admin, alice_user, alice, _ = jobs
    rid, job, _ = service.create(alice, filename="归属漂移任务", category="procurement")
    bobby_user = [u for u in accounts.list_users(admin) if u["username"] == "bobby"][0]
    with accounts._connect() as conn:
        conn.execute("UPDATE reviews SET owner_user_id=? WHERE id=?", (bobby_user["id"], rid))
        before = conn.execute("SELECT data FROM reviews WHERE id=?", (rid,)).fetchone()[0]
    accounts.set_active(admin, alice_user["id"], False)
    assert service.reap_stale() == 0
    with accounts._connect() as conn:
        assert conn.execute("SELECT status FROM owned_review_jobs WHERE id=?", (job,)).fetchone()[0] == "failed"
        assert conn.execute("SELECT data FROM reviews WHERE id=?", (rid,)).fetchone()[0] == before
        assert conn.execute("SELECT owner_user_id FROM reviews WHERE id=?", (rid,)).fetchone()[0] == bobby_user["id"]


def test_non_object_json_quarantined_batch_still_progresses(jobs, monkeypatch):  # noqa: F811
    """codex 二轮 P1-2：合法但非对象的 JSON（null/[]）→ 显式识别隔离，
    健康记录同批推进，二轮幂等；审查原文保留。"""
    accounts, owned, service, admin, alice_user, alice, bob = jobs
    bad_rid, bad_job, _ = service.create(alice, filename="null记录", category="procurement")
    ok_rid, ok_job, _ = service.create(bob, filename="健康记录", category="procurement")
    with accounts._connect() as conn:
        conn.execute("UPDATE reviews SET data='null' WHERE id=?", (bad_rid,))
    accounts.set_active(admin, alice_user["id"], False)
    accounts.set_active(admin, [u for u in accounts.list_users(admin) if u["username"] == "bobby"][0]["id"], False)
    monkeypatch.setattr(maintenance, "_database_path", lambda: accounts.path)
    monkeypatch.setattr(maintenance, "ReviewStore", lambda _p: owned.reviews)
    assert maintenance.recover_once() == (0, 1), "非对象 JSON 不得阻塞整批"
    with accounts._connect() as conn:
        assert conn.execute("SELECT status FROM owned_review_jobs WHERE id=?", (bad_job,)).fetchone()[0] == "failed"
        assert conn.execute("SELECT data FROM reviews WHERE id=?", (bad_rid,)).fetchone()[0] == "null"
    assert maintenance.recover_once() == (0, 0), "二轮幂等"


def test_ledger_orphan_marks_unknown_but_upload_stays_blocked(jobs, monkeypatch):  # noqa: F811
    """codex 二轮 P1-3：孤儿账本行收口 unknown 后，数据级 FK 违例仍在——
    真实 recover_once 成功，但普通上传入口（严格构造）如实保持拦截
    （证据保留优先于恢复上传；修复=离线迁移）。"""
    accounts, owned, service, admin, alice_user, alice, _ = jobs
    ledger = OwnedLLMLedger(service)
    rid, job, secret = service.create(alice, filename="孤儿账本挡门", category="procurement")
    call = ledger.begin(alice, job, secret, node="quality", provider="zhipu", model="glm-5.2")
    with accounts._connect() as conn:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("DELETE FROM owned_llm_attempts WHERE id=?", (call,))
        conn.execute("INSERT INTO owned_llm_attempts (id,job_id,node,provider,model,outcome,started_at) "
                     "VALUES ('orphan-block',?, 'quality','zhipu','glm-5.2','pending',1)", (job,))
        conn.execute("DELETE FROM owned_review_jobs WHERE id=?", (job,))
        conn.execute("DELETE FROM reviews WHERE id=?", (rid,))
    monkeypatch.setattr(maintenance, "_database_path", lambda: accounts.path)
    monkeypatch.setattr(maintenance, "ReviewStore", lambda _p: owned.reviews)
    assert maintenance.recover_once() == (1, 0), "恢复入口正常（孤儿收口 unknown）"
    with accounts._connect() as conn:
        assert conn.execute("SELECT outcome FROM owned_llm_attempts WHERE id='orphan-block'").fetchone()[0] == "unknown"
    import pytest as _pytest
    # 严格拦截来自账本构造器（违例在 owned_llm_attempts 的外键）——
    # 上传入口 _services 依次构造 jobs→ledger，ledger 抛错即上传被拦
    OwnedReviewJobs(owned)  # 任务表本身无违例，构造通过（如实）
    with _pytest.raises(RuntimeError, match="需要离线迁移"):
        OwnedLLMLedger(service)  # 账本严格构造如实保持拦截


def test_reconcile_orphans_fair_share_under_backlog(jobs):  # noqa: F811
    """codex 二轮公平性：普通积压持续占满窗口时孤儿仍有预留份额
    （1/4，至少 1），多轮后孤儿全部收口、不被饿死。"""
    accounts, owned, service, admin, alice_user, alice, bob = jobs
    ledger = OwnedLLMLedger(service)
    orphans = []
    for i in range(2):
        rid, job, secret = service.create(alice, filename=f"孤儿{i}", category="procurement")
        orphans.append(ledger.begin(alice, job, secret, node="quality", provider="zhipu", model="glm-5.2"))
        with accounts._connect() as conn:
            conn.execute("PRAGMA foreign_keys=OFF")
            conn.execute("DELETE FROM owned_review_jobs WHERE id=?", (job,))
            conn.execute("DELETE FROM reviews WHERE id=?", (rid,))
    for i in range(4):
        r, j, s = service.create(bob, filename=f"积压{i}", category="procurement")
        ledger.begin(bob, j, s, node="quality", provider="zhipu", model="glm-5.2")
    accounts.set_active(admin, alice_user["id"], False)
    accounts.set_active(admin, [u for u in accounts.list_users(admin) if u["username"] == "bobby"][0]["id"], False)

    def unknown_orphan_count() -> int:
        with accounts._connect() as conn:
            marks = ",".join("?" * len(orphans))
            return conn.execute(
                f"SELECT COUNT(*) FROM owned_llm_attempts WHERE id IN ({marks}) "
                "AND outcome='unknown'", orphans).fetchone()[0]

    # 多轮：每轮孤儿预留份额≥1，普通积压不再独占窗口
    for _ in range(5):
        ledger.reconcile(limit=4)
        if unknown_orphan_count() == 2:
            break
    assert unknown_orphan_count() == 2, "多轮后孤儿必须全部收口（不被普通积压饿死）"


def test_reconcile_limit_one_is_fair_both_ways(jobs):  # noqa: F811
    """codex 三轮 P2-3：limit=1 小窗口下双向公平——谁更老谁先处理，
    任何一类都无法靠持续到来饿死另一类。"""
    accounts, owned, service, admin, alice_user, alice, bob = jobs
    ledger = OwnedLLMLedger(service)
    # 场景A：旧普通 pending + 新孤儿 → limit=1 普通先收口
    r1, j1, s1 = service.create(bob, filename="旧普通", category="procurement")
    normal_old = ledger.begin(bob, j1, s1, node="quality", provider="zhipu", model="glm-5.2")
    r2, j2, s2 = service.create(alice, filename="新孤儿", category="procurement")
    orphan_new = ledger.begin(alice, j2, s2, node="quality", provider="zhipu", model="glm-5.2")
    with accounts._connect() as conn:
        conn.execute("UPDATE owned_llm_attempts SET started_at=1000 WHERE id=?", (normal_old,))
        conn.execute("UPDATE owned_llm_attempts SET started_at=2000 WHERE id=?", (orphan_new,))
        # 普通侧也要进入失效窗口（租约过期），否则两类的 pending 都不参选
        conn.execute("UPDATE owned_review_jobs SET lease_until=1000 WHERE id=?", (j1,))
    # PRAGMA 在事务内是空操作——单独连接先关外键再删，防级联吞掉孤儿行
    with accounts._connect() as conn:
        conn.execute("PRAGMA foreign_keys=OFF")
        with conn:
            conn.execute("DELETE FROM owned_review_jobs WHERE id=?", (j2,))
            conn.execute("DELETE FROM reviews WHERE id=?", (r2,))
    assert ledger.reconcile(limit=1) == 1
    with accounts._connect() as conn:
        assert conn.execute("SELECT outcome FROM owned_llm_attempts WHERE id=?", (normal_old,)).fetchone()[0] == "unknown"
        assert conn.execute("SELECT outcome FROM owned_llm_attempts WHERE id=?", (orphan_new,)).fetchone()[0] == "pending"
    # 场景B：反过来——旧孤儿 + 新普通 → limit=1 孤儿先收口
    assert ledger.reconcile(limit=1) == 1  # 场景A剩余：新孤儿（此时已是最老）收口
    with accounts._connect() as conn:
        assert conn.execute("SELECT outcome FROM owned_llm_attempts WHERE id=?", (orphan_new,)).fetchone()[0] == "unknown"


def _child_tables_ready(accounts, job_id, *, with_precheck: bool) -> None:
    """给任务行挂一条预审结果子记录（模拟 codex P2-4 的非账本级联数据）。"""
    import time as _time
    with accounts._connect() as conn:
        conn.execute(
            "INSERT INTO owned_review_precheck_claims (job_id, created_at) VALUES (?,?)",
            (job_id, _time.time()))
        if with_precheck:
            conn.execute(
                "INSERT INTO owned_review_precheck_results (job_id,state,original_category,"
                "original_stance,choices_json,versions_json,input_sha256,input_kind,"
                "summary,suspect,performed,precheck_performed,precheck_skip_reason,"
                "suggestion_performed,required_confirmation,created_at) VALUES ("
                "?, 'ready','procurement','neutral','[]','[]','abc','file',"
                "'摘要',0,1,0,NULL,0,0,?)", (job_id, _time.time()))


def test_orphan_with_precheck_evidence_kept(jobs):  # noqa: F811
    """codex 三轮 P2-4：孤儿任务带预审声明/结果（无账本行）→ 保留任务行
    待离线迁移，预审数据（分类/选择/预算/摘要）不随删除丢失。"""
    accounts, owned, service, admin, alice_user, alice, _ = jobs
    rid, job, _ = service.create(alice, filename="带预审孤儿", category="procurement")
    # 建预审子表并挂记录（本库此前未用到预审功能）
    from app.services.owned_review_precheck import _CLAIM_SCHEMA, _RESULT_SCHEMA
    with accounts._connect() as conn:
        conn.execute(_CLAIM_SCHEMA)
        conn.execute(_RESULT_SCHEMA)
    _child_tables_ready(accounts, job, with_precheck=True)
    accounts.set_active(admin, alice_user["id"], False)
    with accounts._connect() as conn:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("DELETE FROM reviews WHERE id=?", (rid,))
    assert service.reap_stale() == 0
    with accounts._connect() as conn:
        assert conn.execute("SELECT status FROM owned_review_jobs WHERE id=?", (job,)).fetchone()[0] == "failed"
        kept = conn.execute("SELECT job_id FROM owned_review_precheck_results WHERE job_id=?", (job,)).fetchone()
        assert kept is not None, "预审结果必须保留（不随孤儿任务删除）"


def test_recovery_logs_distinguish_fk_violation_from_ownership(jobs, monkeypatch, caplog):  # noqa: F811
    """codex 三轮 P2-5：FK 违例报「上传受阻」；归属不一致报「需核查、
    不受阻」——两类分开，日志不给出未经验证的结论。"""
    import logging
    accounts, owned, service, admin, alice_user, alice, _ = jobs
    from app.services.owned_llm_ledger import OwnedLLMLedger
    ledger = OwnedLLMLedger(service)
    # 归属不一致一条（任务+审查都在，owner 漂移）
    rid1, job1, s1 = service.create(alice, filename="归属不一致", category="procurement")
    ledger.begin(alice, job1, s1, node="quality", provider="zhipu", model="glm-5.2")  # 归属不一致调用
    bobby_user = [u for u in accounts.list_users(admin) if u["username"] == "bobby"][0]
    with accounts._connect() as conn:
        conn.execute("UPDATE reviews SET owner_user_id=? WHERE id=?", (bobby_user["id"], rid1))
    # FK 违例一条（审查行缺失）
    rid2, job2, s2 = service.create(alice, filename="真孤儿", category="procurement")
    fk_call = ledger.begin(alice, job2, s2, node="quality", provider="zhipu", model="glm-5.2")
    with accounts._connect() as conn:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("DELETE FROM owned_llm_attempts WHERE id=?", (fk_call,))
        conn.execute("INSERT INTO owned_llm_attempts (id,job_id,node,provider,model,outcome,started_at) "
                     "VALUES ('fk-orphan-x',?, 'quality','zhipu','glm-5.2','pending',1)", (job2,))
        conn.execute("DELETE FROM reviews WHERE id=?", (rid2,))
    accounts.set_active(admin, alice_user["id"], False)
    monkeypatch.setattr(maintenance, "_database_path", lambda: accounts.path)
    monkeypatch.setattr(maintenance, "ReviewStore", lambda _p: owned.reviews)
    with caplog.at_level(logging.ERROR, logger="app.services.owned_review_maintenance"):
        maintenance.recover_once()
    messages = [r.getMessage() for r in caplog.records
                if r.name == "app.services.owned_review_maintenance"]
    joined = "\n".join(messages)
    assert "上传相关检查将拒绝" in joined, "真 FK 违例必须报上传受阻（新口径）"
    assert "数据库存在外键违例" in joined, "事实层（涉及表）单独成行"
    assert "不构成外键违例" in joined and "归属不一致" in joined, "归属不一致必须单独报需核查"
    # 同一条日志不得把两类混在一个结论里
    for line in messages:
        assert not ("孤儿记录" in line and "归属不一致" in line), "两类不得合并报告"


def test_bad_started_at_text_does_not_abort_reconcile(jobs):  # noqa: F811
    """codex 四轮 P1-2：REAL 列中的文本型 started_at（历史损坏）不得让
    Python 排序抛 TypeError 炸整批——健康记录继续推进，坏记录排末尾
    随后收口（外发结果未知语义不变）。"""
    accounts, owned, service, admin, alice_user, alice, bob = jobs
    ledger = OwnedLLMLedger(service)
    r1, j1, s1 = service.create(bob, filename="健康记录", category="procurement")
    healthy = ledger.begin(bob, j1, s1, node="quality", provider="zhipu", model="glm-5.2")
    r2, j2, s2 = service.create(alice, filename="坏时间孤儿", category="procurement")
    bad_call = ledger.begin(alice, j2, s2, node="quality", provider="zhipu", model="glm-5.2")
    with accounts._connect() as conn:
        conn.execute("UPDATE owned_llm_attempts SET started_at='bad' WHERE id=?", (bad_call,))
        # 普通侧进入失效窗口
        conn.execute("UPDATE owned_review_jobs SET lease_until=1 WHERE id=?", (j1,))
    # PRAGMA 在已有事务的连接上是空操作——单独连接先关外键再删，
    # 防级联吞掉孤儿任务行的调用记录
    with accounts._connect() as conn:
        conn.execute("PRAGMA foreign_keys=OFF")
        with conn:
            conn.execute("DELETE FROM owned_review_jobs WHERE id=?", (j2,))
            conn.execute("DELETE FROM reviews WHERE id=?", (r2,))
    # limit=1 双重验证（预留份额语义，codex 五轮 P2-2）：损伤存量优先
    # 清走——坏时间记录占预留份额先收口，健康记录随后推进；两类都不饿死
    assert ledger.reconcile(limit=1) == 1
    with accounts._connect() as conn:
        assert conn.execute("SELECT outcome FROM owned_llm_attempts WHERE id=?", (bad_call,)).fetchone()[0] == "unknown", \
            "坏时间记录占预留份额先收口（损伤存量有限，优先清走）"
        assert conn.execute("SELECT outcome FROM owned_llm_attempts WHERE id=?", (healthy,)).fetchone()[0] == "pending"
    assert ledger.reconcile(limit=1) == 1, "健康记录随后推进"
    with accounts._connect() as conn:
        assert conn.execute("SELECT outcome FROM owned_llm_attempts WHERE id=?", (healthy,)).fetchone()[0] == "unknown"


def test_null_owner_mismatch_is_reported(jobs, monkeypatch, caplog):  # noqa: F811
    """codex 四轮 P2-3：审查行 owner 为 NULL 时，归属不一致（IS NOT 语义）
    必须被报告（!= 会漏报 NULL）；且不构成 FK 违例——不得报上传受阻。"""
    import logging
    accounts, owned, service, admin, alice_user, alice, _ = jobs
    ledger = OwnedLLMLedger(service)
    rid, job, secret = service.create(alice, filename="NULL归属", category="procurement")
    ledger.begin(alice, job, secret, node="quality", provider="zhipu", model="glm-5.2")
    with accounts._connect() as conn:
        conn.execute("UPDATE reviews SET owner_user_id=NULL WHERE id=?", (rid,))
    monkeypatch.setattr(maintenance, "_database_path", lambda: accounts.path)
    monkeypatch.setattr(maintenance, "ReviewStore", lambda _p: owned.reviews)
    with caplog.at_level(logging.ERROR, logger="app.services.owned_review_maintenance"):
        maintenance.recover_once()
    joined = "\n".join(r.getMessage() for r in caplog.records
                       if r.name == "app.services.owned_review_maintenance")
    assert "归属不一致" in joined, "NULL owner 的归属不一致必须被报告（IS NOT 语义）"
    assert "上传相关检查将拒绝" not in joined, "NULL owner 不构成 FK 违例，不得报上传受阻"


def test_missing_account_violation_is_reported(jobs, monkeypatch, caplog):  # noqa: F811
    """codex 四轮 P2-4：任务引用的账户行消失（任务/审查 owner 相同但账户
    已不在）→ 真实外键违例必须如实报告上传受阻（日志只含表名，不含
    合同内容/凭证）。"""
    import logging
    accounts, owned, service, admin, alice_user, alice, _ = jobs
    rid, job, _ = service.create(alice, filename="账户缺失", category="procurement")
    with accounts._connect() as conn:
        conn.execute("PRAGMA foreign_keys=OFF")
        with conn:
            conn.execute("DELETE FROM account_users WHERE id=?", (alice_user["id"],))
    monkeypatch.setattr(maintenance, "_database_path", lambda: accounts.path)
    monkeypatch.setattr(maintenance, "ReviewStore", lambda _p: owned.reviews)
    with caplog.at_level(logging.ERROR, logger="app.services.owned_review_maintenance"):
        maintenance.recover_once()  # 恢复正常完成；违例经 foreign_key_check 探测进日志
    joined = "\n".join(r.getMessage() for r in caplog.records
                       if r.name == "app.services.owned_review_maintenance")
    assert "外键违例" in joined, "缺账户的真实违例必须如实报告"
    assert "上传相关检查将拒绝" in joined, "真实违例必须如实提示上传受阻（新口径）"
    for line in joined.splitlines():
        assert "违约金" not in line and "合同" not in line.replace("账户任务恢复：", ""), \
            "日志不得包含合同内容"


def test_bad_time_candidate_survives_sql_cutoff_both_branches(jobs):  # noqa: F811
    """codex 六轮 P1：SQL 截断前单独选取坏时间候选——同一分支持续加入
    真实 begin() 的正常 pending（占满 SQL 排序窗口）时，坏时间记录仍按
    预留份额收口；普通分支与孤儿分支分别验证。"""
    accounts, owned, service, admin, alice_user, alice, bob = jobs
    ledger = OwnedLLMLedger(service)

    def mark_unknown(call_id: str) -> bool:
        with accounts._connect() as conn:
            row = conn.execute("SELECT outcome FROM owned_llm_attempts WHERE id=?",
                               (call_id,)).fetchone()
        return row is not None and row[0] == "unknown"

    # ── 场景A：普通分支——坏时间 attempt 挂在租约过期的任务上（审查行在），
    # 同分支每轮持续加入真实 begin() 的正常 pending 占满 SQL 窗口
    ra, ja, sa = service.create(bob, filename="普通分支坏时间", category="procurement")
    bad_normal = ledger.begin(bob, ja, sa, node="quality", provider="zhipu", model="glm-5.2")
    with accounts._connect() as conn:
        conn.execute("UPDATE owned_llm_attempts SET started_at='bad' WHERE id=?", (bad_normal,))
        conn.execute("UPDATE owned_review_jobs SET lease_until=1 WHERE id=?", (ja,))
    for _round in range(3):
        r, j, s = service.create(bob, filename=f"普通流量{ _round }", category="procurement")
        ledger.begin(bob, j, s, node="quality", provider="zhipu", model="glm-5.2")
        conn_ts = 100000 + _round
        with accounts._connect() as conn:
            conn.execute("UPDATE owned_llm_attempts SET started_at=? WHERE job_id=?",
                         (conn_ts, j))
            conn.execute("UPDATE owned_review_jobs SET lease_until=1 WHERE id=?", (j,))
        ledger.reconcile(limit=1)
        if mark_unknown(bad_normal):
            break
    assert mark_unknown(bad_normal), "普通分支：持续正常 pending 下坏时间仍须按预留份额收口"

    # ── 场景B：孤儿分支——坏时间 attempt 的任务行已消失，同分支持续加入
    # 数值时间的孤儿 pending 占满 SQL 窗口
    ro, jo, so = service.create(alice, filename="孤儿分支坏时间", category="procurement")
    bad_orphan = ledger.begin(alice, jo, so, node="quality", provider="zhipu", model="glm-5.2")
    # 制造真孤儿：任务行+审查行消失（attempt 行保留且 started_at 为文本）
    with accounts._connect() as conn:
        conn.execute("PRAGMA foreign_keys=OFF")
        with conn:
            conn.execute("DELETE FROM owned_review_jobs WHERE id=?", (jo,))
            conn.execute("DELETE FROM reviews WHERE id=?", (ro,))
    with accounts._connect() as conn:
        conn.execute("UPDATE owned_llm_attempts SET started_at='bad' WHERE id=?", (bad_orphan,))
    for _round in range(3):
        r, j, s = service.create(alice, filename=f"孤儿流量{ _round }", category="procurement")
        ledger.begin(alice, j, s, node="quality", provider="zhipu", model="glm-5.2")
        with accounts._connect() as conn:
            conn.execute("UPDATE owned_llm_attempts SET started_at=? WHERE job_id=?",
                         (200000 + _round, j))
        # PRAGMA 在已有事务的连接上是空操作——单独连接先关外键再删
        with accounts._connect() as conn:
            conn.execute("PRAGMA foreign_keys=OFF")
            with conn:
                conn.execute("DELETE FROM owned_review_jobs WHERE id=?", (j,))
                conn.execute("DELETE FROM reviews WHERE id=?", (r,))
        ledger.reconcile(limit=1)
        if mark_unknown(bad_orphan):
            break
    assert mark_unknown(bad_orphan), "孤儿分支：持续孤儿 pending 下坏时间仍须按预留份额收口"



def _child_tables_ready(accounts, job_id, *, with_precheck: bool) -> None:
    """给任务行挂一条预审结果子记录（模拟 codex P2-4 的非账本级联数据）。"""
    import time as _time
    with accounts._connect() as conn:
        conn.execute(
            "INSERT INTO owned_review_precheck_claims (job_id, created_at) VALUES (?,?)",
            (job_id, _time.time()))
        if with_precheck:
            conn.execute(
                "INSERT INTO owned_review_precheck_results (job_id,state,original_category,"
                "original_stance,choices_json,versions_json,input_sha256,input_kind,"
                "summary,suspect,performed,precheck_performed,precheck_skip_reason,"
                "suggestion_performed,required_confirmation,created_at) VALUES ("
                "?, 'ready','procurement','neutral','[]','[]','abc','file',"
                "'摘要',0,1,0,NULL,0,0,?)", (job_id, _time.time()))


def test_orphan_with_precheck_evidence_kept(jobs):  # noqa: F811
    """codex 三轮 P2-4：孤儿任务带预审声明/结果（无账本行）→ 保留任务行
    待离线迁移，预审数据（分类/选择/预算/摘要）不随删除丢失。"""
    accounts, owned, service, admin, alice_user, alice, _ = jobs
    rid, job, _ = service.create(alice, filename="带预审孤儿", category="procurement")
    # 建预审子表并挂记录（本库此前未用到预审功能）
    from app.services.owned_review_precheck import _CLAIM_SCHEMA, _RESULT_SCHEMA
    with accounts._connect() as conn:
        conn.execute(_CLAIM_SCHEMA)
        conn.execute(_RESULT_SCHEMA)
    _child_tables_ready(accounts, job, with_precheck=True)
    accounts.set_active(admin, alice_user["id"], False)
    with accounts._connect() as conn:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("DELETE FROM reviews WHERE id=?", (rid,))
    assert service.reap_stale() == 0
    with accounts._connect() as conn:
        assert conn.execute("SELECT status FROM owned_review_jobs WHERE id=?", (job,)).fetchone()[0] == "failed"
        kept = conn.execute("SELECT job_id FROM owned_review_precheck_results WHERE job_id=?", (job,)).fetchone()
        assert kept is not None, "预审结果必须保留（不随孤儿任务删除）"


def test_recovery_logs_distinguish_fk_violation_from_ownership(jobs, monkeypatch, caplog):  # noqa: F811
    """codex 三轮 P2-5：FK 违例报「上传受阻」；归属不一致报「需核查、
    不受阻」——两类分开，日志不给出未经验证的结论。"""
    import logging
    accounts, owned, service, admin, alice_user, alice, _ = jobs
    from app.services.owned_llm_ledger import OwnedLLMLedger
    ledger = OwnedLLMLedger(service)
    # 归属不一致一条（任务+审查都在，owner 漂移）
    rid1, job1, s1 = service.create(alice, filename="归属不一致", category="procurement")
    ledger.begin(alice, job1, s1, node="quality", provider="zhipu", model="glm-5.2")  # 归属不一致调用
    bobby_user = [u for u in accounts.list_users(admin) if u["username"] == "bobby"][0]
    with accounts._connect() as conn:
        conn.execute("UPDATE reviews SET owner_user_id=? WHERE id=?", (bobby_user["id"], rid1))
    # FK 违例一条（审查行缺失）
    rid2, job2, s2 = service.create(alice, filename="真孤儿", category="procurement")
    fk_call = ledger.begin(alice, job2, s2, node="quality", provider="zhipu", model="glm-5.2")
    with accounts._connect() as conn:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("DELETE FROM owned_llm_attempts WHERE id=?", (fk_call,))
        conn.execute("INSERT INTO owned_llm_attempts (id,job_id,node,provider,model,outcome,started_at) "
                     "VALUES ('fk-orphan-x',?, 'quality','zhipu','glm-5.2','pending',1)", (job2,))
        conn.execute("DELETE FROM reviews WHERE id=?", (rid2,))
    accounts.set_active(admin, alice_user["id"], False)
    monkeypatch.setattr(maintenance, "_database_path", lambda: accounts.path)
    monkeypatch.setattr(maintenance, "ReviewStore", lambda _p: owned.reviews)
    with caplog.at_level(logging.ERROR, logger="app.services.owned_review_maintenance"):
        maintenance.recover_once()
    messages = [r.getMessage() for r in caplog.records
                if r.name == "app.services.owned_review_maintenance"]
    joined = "\n".join(messages)
    assert "上传相关检查将拒绝" in joined, "真 FK 违例必须报上传受阻（新口径）"
    assert "数据库存在外键违例" in joined, "事实层（涉及表）单独成行"
    assert "不构成外键违例" in joined and "归属不一致" in joined, "归属不一致必须单独报需核查"
    # 同一条日志不得把两类混在一个结论里
    for line in messages:
        assert not ("孤儿记录" in line and "归属不一致" in line), "两类不得合并报告"


def test_bad_started_at_text_does_not_abort_reconcile(jobs):  # noqa: F811
    """codex 四轮 P1-2：REAL 列中的文本型 started_at（历史损坏）不得让
    Python 排序抛 TypeError 炸整批——健康记录继续推进，坏记录排末尾
    随后收口（外发结果未知语义不变）。"""
    accounts, owned, service, admin, alice_user, alice, bob = jobs
    ledger = OwnedLLMLedger(service)
    r1, j1, s1 = service.create(bob, filename="健康记录", category="procurement")
    healthy = ledger.begin(bob, j1, s1, node="quality", provider="zhipu", model="glm-5.2")
    r2, j2, s2 = service.create(alice, filename="坏时间孤儿", category="procurement")
    bad_call = ledger.begin(alice, j2, s2, node="quality", provider="zhipu", model="glm-5.2")
    with accounts._connect() as conn:
        conn.execute("UPDATE owned_llm_attempts SET started_at='bad' WHERE id=?", (bad_call,))
        # 普通侧进入失效窗口
        conn.execute("UPDATE owned_review_jobs SET lease_until=1 WHERE id=?", (j1,))
    # PRAGMA 在已有事务的连接上是空操作——单独连接先关外键再删，
    # 防级联吞掉孤儿任务行的调用记录
    with accounts._connect() as conn:
        conn.execute("PRAGMA foreign_keys=OFF")
        with conn:
            conn.execute("DELETE FROM owned_review_jobs WHERE id=?", (j2,))
            conn.execute("DELETE FROM reviews WHERE id=?", (r2,))
    # limit=1 双重验证（预留份额语义，codex 五轮 P2-2）：损伤存量优先
    # 清走——坏时间记录占预留份额先收口，健康记录随后推进；两类都不饿死
    assert ledger.reconcile(limit=1) == 1
    with accounts._connect() as conn:
        assert conn.execute("SELECT outcome FROM owned_llm_attempts WHERE id=?", (bad_call,)).fetchone()[0] == "unknown", \
            "坏时间记录占预留份额先收口（损伤存量有限，优先清走）"
        assert conn.execute("SELECT outcome FROM owned_llm_attempts WHERE id=?", (healthy,)).fetchone()[0] == "pending"
    assert ledger.reconcile(limit=1) == 1, "健康记录随后推进"
    with accounts._connect() as conn:
        assert conn.execute("SELECT outcome FROM owned_llm_attempts WHERE id=?", (healthy,)).fetchone()[0] == "unknown"


def test_null_owner_mismatch_is_reported(jobs, monkeypatch, caplog):  # noqa: F811
    """codex 四轮 P2-3：审查行 owner 为 NULL 时，归属不一致（IS NOT 语义）
    必须被报告（!= 会漏报 NULL）；且不构成 FK 违例——不得报上传受阻。"""
    import logging
    accounts, owned, service, admin, alice_user, alice, _ = jobs
    ledger = OwnedLLMLedger(service)
    rid, job, secret = service.create(alice, filename="NULL归属", category="procurement")
    ledger.begin(alice, job, secret, node="quality", provider="zhipu", model="glm-5.2")
    with accounts._connect() as conn:
        conn.execute("UPDATE reviews SET owner_user_id=NULL WHERE id=?", (rid,))
    monkeypatch.setattr(maintenance, "_database_path", lambda: accounts.path)
    monkeypatch.setattr(maintenance, "ReviewStore", lambda _p: owned.reviews)
    with caplog.at_level(logging.ERROR, logger="app.services.owned_review_maintenance"):
        maintenance.recover_once()
    joined = "\n".join(r.getMessage() for r in caplog.records
                       if r.name == "app.services.owned_review_maintenance")
    assert "归属不一致" in joined, "NULL owner 的归属不一致必须被报告（IS NOT 语义）"
    assert "上传相关检查将拒绝" not in joined, "NULL owner 不构成 FK 违例，不得报上传受阻"


def test_missing_account_violation_is_reported(jobs, monkeypatch, caplog):  # noqa: F811
    """codex 四轮 P2-4：任务引用的账户行消失（任务/审查 owner 相同但账户
    已不在）→ 真实外键违例必须如实报告上传受阻（日志只含表名，不含
    合同内容/凭证）。"""
    import logging
    accounts, owned, service, admin, alice_user, alice, _ = jobs
    rid, job, _ = service.create(alice, filename="账户缺失", category="procurement")
    with accounts._connect() as conn:
        conn.execute("PRAGMA foreign_keys=OFF")
        with conn:
            conn.execute("DELETE FROM account_users WHERE id=?", (alice_user["id"],))
    monkeypatch.setattr(maintenance, "_database_path", lambda: accounts.path)
    monkeypatch.setattr(maintenance, "ReviewStore", lambda _p: owned.reviews)
    with caplog.at_level(logging.ERROR, logger="app.services.owned_review_maintenance"):
        maintenance.recover_once()  # 恢复正常完成；违例经 foreign_key_check 探测进日志
    joined = "\n".join(r.getMessage() for r in caplog.records
                       if r.name == "app.services.owned_review_maintenance")
    assert "外键违例" in joined, "缺账户的真实违例必须如实报告"
    assert "上传相关检查将拒绝" in joined, "真实违例必须如实提示上传受阻（新口径）"
    for line in joined.splitlines():
        assert "违约金" not in line and "合同" not in line.replace("账户任务恢复：", ""), \
            "日志不得包含合同内容"


def test_bad_time_orphan_closes_under_sustained_traffic(jobs):  # noqa: F811
    """codex 五轮 P2-2 持续流量场景：每轮都有新的正常失效记录进入、
    limit=1 逐条处理——坏时间孤儿靠预留份额在首轮即收口（损伤存量
    有限、优先清走），不再被持续积压推到末尾饿死。"""
    accounts, owned, service, admin, alice_user, alice, bob = jobs
    ledger = OwnedLLMLedger(service)
    r_bad, j_bad, s_bad = service.create(alice, filename="坏时间", category="procurement")
    bad_call = ledger.begin(alice, j_bad, s_bad, node="quality", provider="zhipu", model="glm-5.2")
    with accounts._connect() as conn:
        conn.execute("UPDATE owned_llm_attempts SET started_at='bad' WHERE id=?", (bad_call,))
    # 预置持续流量源：bob 的 3 条任务轮番失效进入
    for i in range(3):
        service.create(bob, filename=f"流量{i}", category="procurement")
    accounts.set_active(admin, alice_user["id"], False)
    accounts.set_active(admin, [u for u in accounts.list_users(admin) if u["username"] == "bobby"][0]["id"], False)
    for _round in range(4):
        ledger.reconcile(limit=1)
        with accounts._connect() as conn:
            state = conn.execute("SELECT outcome FROM owned_llm_attempts WHERE id=?", (bad_call,)).fetchone()[0]
        if state == "unknown":
            break
    assert state == "unknown", "持续积压下坏时间孤儿必须最终收口"
