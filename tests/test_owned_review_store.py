"""Same-database ownership primitives; these do not test the existing HTTP routes."""
from concurrent.futures import ThreadPoolExecutor
import sqlite3
from threading import Event

import pytest

from app.services.account_store import AccountStore, AuthenticationError
from app.services.owned_review_store import OwnedReviewStore
from app.services.store import ReviewStore
from tests.helpers import create_active_account


@pytest.fixture
def owned(tmp_path):
    path = tmp_path / "isolated.sqlite"
    accounts = AccountStore(path)
    accounts.bootstrap_admin("admin", "管理员", "testing-password-123")
    admin = accounts.login("admin", "testing-password-123")
    alice = create_active_account(accounts, admin, "alice", "甲", "alice-password-123", "user")
    create_active_account(accounts, admin, "bobby", "乙", "bobby-password-123", "user")
    reviews = ReviewStore(str(path))
    legacy = reviews.create(filename="旧合同", status="done")
    return accounts, reviews, OwnedReviewStore(accounts, reviews), admin, alice, accounts.login("alice", "alice-password-123"), accounts.login("bobby", "bobby-password-123"), legacy


def test_owners_filter_before_limit_and_legacy_and_admin_have_no_access(owned):
    _, _, store, admin, _, alice, bob, legacy = owned
    alice_id = store.create(alice, filename="甲合同", status="done", text="甲方正文")
    for index in range(3):
        store.create(bob, filename=f"乙合同{index}", status="done")
    assert store.get(alice, alice_id)["text"] == "甲方正文"
    assert store.get(bob, alice_id) is None
    assert store.get(admin, alice_id) is None
    assert store.get(admin, legacy) is None and store.get(alice, legacy) is None
    assert [row["id"] for row in store.list_reviews(alice, limit=1)] == [alice_id]
    assert store.mutate(bob, alice_id, lambda row: row.update(text="越权")) is None
    assert store.get(alice, alice_id)["text"] == "甲方正文"


def test_owner_and_identifier_cannot_be_assigned_by_caller(owned):
    _, _, store, _, _, alice, _, _ = owned
    with pytest.raises(ValueError):
        store.create(alice, owner_user_id="other")
    rid = store.create(alice, filename="合同")
    with pytest.raises(ValueError):
        store.mutate(alice, rid, lambda row: row.update(id="different"))
    with pytest.raises(ValueError):
        store.mutate(alice, rid, lambda row: row.update(owner_user_id="other"))
    assert store.get(alice, rid)["id"] == rid


def test_account_deactivation_serializes_with_contract_mutation(owned):
    accounts, _, store, admin, alice_user, alice, _, _ = owned
    rid = store.create(alice, filename="合同", text="原内容")
    entered, release, revoke_started = Event(), Event(), Event()

    def update():
        def change(row):
            entered.set()
            assert release.wait(5)
            row["text"] = "撤权前的修改"
        return store.mutate(alice, rid, change)

    def revoke():
        revoke_started.set()
        accounts.set_active(admin, alice_user["id"], False)

    with ThreadPoolExecutor(max_workers=2) as pool:
        updating = pool.submit(update)
        assert entered.wait(5)
        revoking = pool.submit(revoke)
        assert revoke_started.wait(5)
        release.set()
        assert updating.result()["text"] == "撤权前的修改"
        revoking.result()
    with pytest.raises(AuthenticationError):
        store.mutate(alice, rid, lambda row: row.update(text="撤权后的修改"))
    with pytest.raises(AuthenticationError):
        store.get(alice, rid)


def test_rejects_separate_databases(tmp_path):
    accounts = AccountStore(tmp_path / "accounts.sqlite")
    reviews = ReviewStore(str(tmp_path / "reviews.sqlite"))
    with pytest.raises(ValueError, match="同一数据库"):
        OwnedReviewStore(accounts, reviews)


def test_expired_owned_review_is_deleted_with_known_related_data(owned):
    accounts, reviews, store, _, _, alice, _, _ = owned
    reviews._ttl = 10
    rid = store.create(alice, filename="过期合同")
    with accounts._connect() as conn:
        conn.execute("CREATE TABLE ai_finding_status (review_id TEXT, finding_key TEXT)")
        conn.execute("INSERT INTO ai_finding_status VALUES (?,?)", (rid, "finding"))
        conn.execute("CREATE TABLE llm_calls (review_id TEXT, call_id TEXT)")
        conn.execute("INSERT INTO llm_calls VALUES (?,?)", (rid, "call"))
        conn.execute("UPDATE reviews SET created_at=0 WHERE id=?", (rid,))

    assert store.get(alice, rid) is None
    with accounts._connect() as conn:
        assert conn.execute("SELECT 1 FROM reviews WHERE id=?", (rid,)).fetchone() is None
        assert conn.execute("SELECT 1 FROM ai_finding_status WHERE review_id=?", (rid,)).fetchone() is None
        assert conn.execute("SELECT 1 FROM llm_calls WHERE review_id=?", (rid,)).fetchone() is None


def test_failed_known_related_cleanup_rolls_back_review_deletion(owned):
    accounts, reviews, store, _, _, alice, _, _ = owned
    reviews._ttl = 10
    rid = store.create(alice, filename="过期合同")
    with accounts._connect() as conn:
        conn.execute("CREATE TABLE ai_finding_status (review_id TEXT)")
        conn.execute("INSERT INTO ai_finding_status VALUES (?)", (rid,))
        conn.execute("CREATE TRIGGER reject_cleanup BEFORE DELETE ON ai_finding_status BEGIN SELECT RAISE(ABORT, 'cleanup failed'); END")
        conn.execute("UPDATE reviews SET created_at=0 WHERE id=?", (rid,))
    with pytest.raises(sqlite3.IntegrityError, match="cleanup failed"):
        store.get(alice, rid)
    with accounts._connect() as conn:
        assert conn.execute("SELECT 1 FROM reviews WHERE id=?", (rid,)).fetchone() is not None
        assert conn.execute("SELECT 1 FROM ai_finding_status WHERE review_id=?", (rid,)).fetchone() is not None


def test_unknown_cleanup_hook_blocks_deletion_without_executing_it(owned, monkeypatch):
    from app.services import store as store_module

    accounts, reviews, store, _, _, alice, _, _ = owned
    reviews._ttl = 10
    rid = store.create(alice, filename="过期合同")
    with accounts._connect() as conn:
        conn.execute("CREATE TABLE ai_finding_status (review_id TEXT)")
        conn.execute("INSERT INTO ai_finding_status VALUES (?)", (rid,))
        conn.execute("UPDATE reviews SET created_at=0 WHERE id=?", (rid,))

    called = []

    def failing_hook(conn, ids):
        called.append(True)
        conn.commit()

    monkeypatch.setattr(store_module, "PURGE_HOOKS", [failing_hook])
    with pytest.raises(RuntimeError, match="未纳入事务清理"):
        store.get(alice, rid)
    assert not called
    with accounts._connect() as conn:
        assert conn.execute("SELECT 1 FROM reviews WHERE id=?", (rid,)).fetchone() is not None
        assert conn.execute("SELECT 1 FROM ai_finding_status WHERE review_id=?", (rid,)).fetchone() is not None


def test_expiry_cleans_actual_related_schemas_without_deleting_other_reviews(owned, monkeypatch):
    from app.services import ai_findings, impact, llm_call_log, remediation_budget, revision

    accounts, reviews, store, _, _, alice, _, _ = owned
    reviews._ttl = 10
    expired = store.create(alice, filename="过期合同")
    current = store.create(alice, filename="有效合同")
    monkeypatch.setattr(revision, "_db_path", lambda: str(accounts.path))
    with revision._connect() as conn:
        pass
    with accounts._connect() as conn:
        conn.execute(ai_findings._STATUS_DDL)
        conn.execute(impact._SELECTED_DDL)
        conn.execute(impact._IMPACT_REPORTS_DDL)
        remediation_budget._create(conn)
        llm_call_log._ensure_table(conn)
        for rid in (expired, current):
            conn.execute("INSERT INTO ai_finding_status VALUES (?,?,?,?)", (rid, "finding", "viewed", 1))
            conn.execute("INSERT INTO llm_calls (call_id, review_id, node, provider, model, outcome, created_at) VALUES (?,?,?,?,?,?,?)", (rid, rid, "ask", "test", "test", "success", 1))
            conn.execute("INSERT INTO remediation_calls (review_id,item_id,attempts,state) VALUES (?,?,?,?)", (rid, "item", 1, "done"))
            conn.execute("INSERT INTO revision_budget (review_id) VALUES (?)", (rid,))
            conn.execute("INSERT INTO revision_drafts (id,review_id,item_id,created_at,payload,status) VALUES (?,?,?,?,?,?)", (rid, rid, "item", 1, "{}", "ok"))
            conn.execute("INSERT INTO selected_drafts VALUES (?,?,?,?,?)", (rid, "item", rid, "safe", 1))
            conn.execute("INSERT INTO impact_reports (id,review_id,item_id,draft_id,created_at,payload,status) VALUES (?,?,?,?,?,?,?)", (rid, rid, "item", rid, 1, "{}", "ok"))
        conn.execute("UPDATE reviews SET created_at=0 WHERE id=?", (expired,))
    assert store.get(alice, expired) is None
    with accounts._connect() as conn:
        for table in ("ai_finding_status", "llm_calls", "remediation_calls", "revision_budget",
                      "revision_drafts", "selected_drafts", "impact_reports"):
            assert conn.execute(f'SELECT COUNT(*) FROM "{table}" WHERE review_id=?', (expired,)).fetchone()[0] == 0
            assert conn.execute(f'SELECT COUNT(*) FROM "{table}" WHERE review_id=?', (current,)).fetchone()[0] == 1


def test_create_cleans_expired_unbound_and_orphan_logs_but_keeps_live_or_recent(owned):
    accounts, reviews, store, _, _, alice, _, _ = owned
    reviews._ttl = 10
    live = store.create(alice, filename="有效合同")
    with accounts._connect() as conn:
        conn.execute("CREATE TABLE llm_calls (call_id TEXT PRIMARY KEY, review_id TEXT, created_at REAL)")
        conn.executemany("INSERT INTO llm_calls VALUES (?,?,?)", [
            ("old_unbound", None, 0), ("old_orphan", "missing", 0),
            ("old_live", live, 0), ("recent_unbound", None, 9999999999),
        ])
    store.create(alice, filename="触发过期清理")
    with accounts._connect() as conn:
        remaining = {row[0] for row in conn.execute("SELECT call_id FROM llm_calls")}
    assert remaining == {"old_live", "recent_unbound"}


def test_unknown_hook_blocks_orphan_log_cleanup_and_new_review(owned, monkeypatch):
    from app.services import store as store_module

    accounts, reviews, store, _, _, alice, _, _ = owned
    reviews._ttl = 10
    with accounts._connect() as conn:
        conn.execute("CREATE TABLE llm_calls (call_id TEXT PRIMARY KEY, review_id TEXT, created_at REAL)")
        conn.execute("INSERT INTO llm_calls VALUES ('old', NULL, 0)")
        before = conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0]
    monkeypatch.setattr(store_module, "PURGE_HOOKS", [lambda conn, ids: None])
    with pytest.raises(RuntimeError, match="未纳入事务清理"):
        store.create(alice, filename="不应创建")
    with accounts._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0] == before
        assert conn.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0] == 1


def test_orphan_cleanup_is_bounded_and_sql_failure_rolls_back_new_review(owned):
    accounts, reviews, store, _, _, alice, _, _ = owned
    reviews._ttl = 10
    with accounts._connect() as conn:
        conn.execute("CREATE TABLE llm_calls (call_id TEXT PRIMARY KEY, review_id TEXT, created_at REAL)")
        conn.executemany("INSERT INTO llm_calls VALUES (?,NULL,0)", [(f"old-{i:03d}",) for i in range(101)])
    store.create(alice, filename="第一批")
    with accounts._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0] == 1
        before = conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0]
        conn.execute("CREATE TRIGGER reject_log_cleanup BEFORE DELETE ON llm_calls BEGIN SELECT RAISE(ABORT, 'log cleanup failed'); END")
    with pytest.raises(sqlite3.IntegrityError, match="log cleanup failed"):
        store.create(alice, filename="不应创建")
    with accounts._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0] == before
        assert conn.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0] == 1
        conn.execute("DROP TRIGGER reject_log_cleanup")
    store.create(alice, filename="第二批")
    with accounts._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0] == 0


def test_session_expiring_during_mutation_rolls_back(owned, monkeypatch):
    from app.services import account_store

    accounts, _, store, _, _, alice, _, _ = owned
    rid = store.create(alice, filename="合同", text="原内容")
    now = account_store.time.time()
    clock = [now]
    monkeypatch.setattr(account_store.time, "time", lambda: clock[0])

    def expire_before_commit(row):
        row["text"] = "不应提交"
        clock[0] = now + 9 * 60 * 60

    with pytest.raises(AuthenticationError):
        store.mutate(alice, rid, expire_before_commit)
    with accounts._connect() as conn:
        assert "不应提交" not in conn.execute("SELECT data FROM reviews WHERE id=?", (rid,)).fetchone()[0]


def test_review_expiring_during_mutation_is_deleted_instead_of_returned(owned, monkeypatch):
    from app.services import account_store

    accounts, reviews, store, _, _, alice, _, _ = owned
    reviews._ttl = 10
    rid = store.create(alice, filename="合同", text="原内容")
    now = account_store.time.time()
    clock = [now]
    monkeypatch.setattr(account_store.time, "time", lambda: clock[0])

    def expire_review(row):
        row["text"] = "过期后不应返回"
        clock[0] = now + 11

    assert store.mutate(alice, rid, expire_review) is None
    with accounts._connect() as conn:
        assert conn.execute("SELECT 1 FROM reviews WHERE id=?", (rid,)).fetchone() is None
