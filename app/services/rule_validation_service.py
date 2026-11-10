"""Bind server-run local gold-test results to an authenticated candidate.

Gold checks are a partial local verification, not a publication authorization.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
import uuid
from contextlib import closing
from dataclasses import asdict
from typing import Any

from app.services.account_store import AccountStore, AuthorizationError
from app.services.rule_candidate_validation import GoldCheckResult, process_control_uncertain, run_gold_checks
from app.services.rule_snapshot import RuleSnapshot
from app.services.rule_submission import RuleSubmissionService

_VALIDATION_SLOTS = threading.BoundedSemaphore(1)


class RuleValidationService:
    def __init__(self, accounts: AccountStore) -> None:
        self.accounts = accounts
        self.submissions = RuleSubmissionService(accounts)
        with closing(accounts._connect()) as conn, conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS rule_validation_results (
                    id TEXT PRIMARY KEY, candidate_id TEXT NOT NULL REFERENCES rule_candidates(id),
                    candidate_sha256 TEXT NOT NULL, base_generation INTEGER NOT NULL,
                    actor_id TEXT NOT NULL REFERENCES account_users(id), created_at REAL NOT NULL,
                    result_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS rule_validation_jobs (
                    id TEXT PRIMARY KEY, candidate_id TEXT NOT NULL REFERENCES rule_candidates(id),
                    actor_id TEXT NOT NULL REFERENCES account_users(id), status TEXT NOT NULL,
                    started_at REAL NOT NULL, lease_until REAL NOT NULL, result_id TEXT,
                    CHECK(status IN ('running','completed','failed','abandoned'))
                );
                CREATE TABLE IF NOT EXISTS rule_validation_control (
                    id INTEGER PRIMARY KEY CHECK(id=1), blocked INTEGER NOT NULL CHECK(blocked IN (0,1)),
                    reason TEXT NOT NULL, updated_at REAL NOT NULL, last_job_id TEXT
                );
                INSERT OR IGNORE INTO rule_validation_control(id,blocked,reason,updated_at) VALUES (1,0,'',0);
                CREATE TRIGGER IF NOT EXISTS validation_control_no_delete BEFORE DELETE ON rule_validation_control
                BEGIN SELECT RAISE(ABORT, 'validation control is retained'); END;
                CREATE TRIGGER IF NOT EXISTS validation_control_no_online_reset BEFORE UPDATE ON rule_validation_control
                WHEN OLD.blocked=1 AND NEW.blocked=0
                BEGIN SELECT RAISE(ABORT, 'validation control requires offline recovery'); END;
                CREATE UNIQUE INDEX IF NOT EXISTS one_running_rule_validation
                    ON rule_validation_jobs ((1)) WHERE status='running';
                CREATE TRIGGER IF NOT EXISTS validation_job_no_delete BEFORE DELETE ON rule_validation_jobs
                BEGIN SELECT RAISE(ABORT, 'validation jobs are retained'); END;
                CREATE TRIGGER IF NOT EXISTS validation_job_no_replace BEFORE INSERT ON rule_validation_jobs
                WHEN EXISTS (SELECT 1 FROM rule_validation_jobs WHERE id=NEW.id)
                BEGIN SELECT RAISE(ABORT, 'validation jobs are retained'); END;
                CREATE TRIGGER IF NOT EXISTS validation_job_valid_insert BEFORE INSERT ON rule_validation_jobs
                WHEN NEW.status != 'running' OR NEW.result_id IS NOT NULL OR
                     EXISTS (SELECT 1 FROM rule_validation_jobs WHERE status='running')
                BEGIN SELECT RAISE(ABORT, 'invalid or concurrent validation job'); END;
                CREATE TRIGGER IF NOT EXISTS validation_job_transition BEFORE UPDATE ON rule_validation_jobs
                WHEN OLD.status != 'running' OR NEW.status NOT IN ('completed','failed','abandoned') OR
                     NEW.id != OLD.id OR NEW.candidate_id != OLD.candidate_id OR NEW.actor_id != OLD.actor_id OR
                     NEW.started_at != OLD.started_at OR NEW.lease_until != OLD.lease_until OR
                     (NEW.status = 'completed' AND (NEW.result_id IS NULL OR NOT EXISTS (
                         SELECT 1 FROM rule_validation_results WHERE id=NEW.result_id AND candidate_id=NEW.candidate_id
                     ))) OR
                     (NEW.status != 'completed' AND NEW.result_id IS NOT NULL)
                BEGIN SELECT RAISE(ABORT, 'invalid validation job transition'); END;
                CREATE TRIGGER IF NOT EXISTS validation_no_update BEFORE UPDATE ON rule_validation_results
                BEGIN SELECT RAISE(ABORT, 'validation results are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS validation_no_delete BEFORE DELETE ON rule_validation_results
                BEGIN SELECT RAISE(ABORT, 'validation results are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS validation_no_replace BEFORE INSERT ON rule_validation_results
                WHEN EXISTS (SELECT 1 FROM rule_validation_results WHERE id=NEW.id)
                BEGIN SELECT RAISE(ABORT, 'validation results are append-only'); END;
            """)
            conn.execute("BEGIN IMMEDIATE")
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(rule_validation_control)")}
            if "last_job_id" not in columns:
                conn.execute("ALTER TABLE rule_validation_control ADD COLUMN last_job_id TEXT")

    def _candidate(self, conn: sqlite3.Connection, token: str, candidate_id: str, *, current: bool) -> tuple[sqlite3.Row, sqlite3.Row]:
        actor = self.accounts._session(conn, token)
        row = conn.execute("SELECT c.* FROM rule_candidates c JOIN rule_candidate_details d ON d.candidate_id=c.id "
                           "WHERE c.id=?", (candidate_id,)).fetchone()
        if row is None:
            raise ValueError("候选规则不存在")
        if actor["role"] != "admin" and actor["id"] != row["submitted_by"]:
            raise AuthorizationError("无权查看该候选规则")
        if row["base_generation"] is None:
            raise ValueError("候选规则缺少可信基线代次")
        if current:
            review = conn.execute("SELECT decision FROM rule_candidate_reviews WHERE candidate_id=? ORDER BY rowid DESC LIMIT 1", (candidate_id,)).fetchone()
            if review and review["decision"] == "reject":
                raise ValueError("候选规则已退回，请重新提交")
        if (hashlib.sha256(row["content"]).hexdigest() != row["sha256"] or
                hashlib.sha256(row["baseline"]).hexdigest() != row["base_sha256"]):
            raise ValueError("候选规则内容损坏")
        if current:
            baseline, generation = self.submissions.ledger._baseline(conn, row["category"])
            if generation != row["base_generation"] or hashlib.sha256(baseline).hexdigest() != row["base_sha256"]:
                raise ValueError("规则基线代次已变化")
        return actor, row

    def _arm_control(self, conn: sqlite3.Connection, reason: str, job_id: str, now: float) -> None:
        # 收尾事务内原子落闸：已阻断时保留原原因；写闸失败抛错使整个事务回滚，任务保持 running
        cursor = conn.execute(
            "UPDATE rule_validation_control SET blocked=1, "
            "reason=CASE WHEN blocked=1 THEN reason ELSE ? END, "
            "updated_at=CASE WHEN blocked=1 THEN updated_at ELSE ? END, "
            "last_job_id=CASE WHEN blocked=1 THEN last_job_id ELSE ? END WHERE id=1",
            (reason, now, job_id),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("测试安全状态不可用")

    def validate(self, token: str, candidate_id: str) -> dict[str, Any]:
        self.accounts.authenticate(token)
        if not _VALIDATION_SLOTS.acquire(blocking=False):
            raise RuntimeError("已有候选规则正在测试，请稍后重试")
        job_id = None
        unsafe = False
        try:
            recovery_needed = False
            with closing(self.accounts._connect()) as conn, conn:
                conn.execute("BEGIN IMMEDIATE")
                actor, candidate = self._candidate(conn, token, candidate_id, current=True)
                actor_id, digest, generation = actor["id"], candidate["sha256"], candidate["base_generation"]
                snapshot = RuleSnapshot(candidate["category"], candidate["content"], digest[:12])
                now = time.time()
                control = conn.execute("SELECT blocked FROM rule_validation_control WHERE id=1").fetchone()
                if control is None or control["blocked"]:
                    raise RuntimeError("测试进程状态待检查，暂不能启动新测试")
                expired = conn.execute("SELECT id FROM rule_validation_jobs WHERE status='running' AND lease_until<=?", (now,)).fetchone()
                if expired:
                    conn.execute("UPDATE rule_validation_jobs SET status='abandoned' WHERE status='running' AND lease_until<=?", (now,))
                    conn.execute("UPDATE rule_validation_control SET blocked=1,reason='expired_job',updated_at=?,last_job_id=? WHERE id=1", (now, expired["id"]))
                    recovery_needed = True
                elif conn.execute("SELECT 1 FROM rule_validation_jobs WHERE status='running'").fetchone():
                    raise RuntimeError("已有候选规则正在测试，请稍后重试")
                else:
                    job_id = uuid.uuid4().hex
                    conn.execute("INSERT INTO rule_validation_jobs VALUES (?,?,?,?,?,?,?)", (
                        job_id, candidate_id, actor_id, "running", now, now + 600, None,
                    ))
            if recovery_needed:
                raise RuntimeError("上次测试任务已中断，请检查进程与工作区后再恢复")
            result = run_gold_checks(snapshot)
            unsafe = process_control_uncertain() or (isinstance(result, GoldCheckResult) and result.exit_code is None)
            if unsafe:
                with closing(self.accounts._connect()) as conn, conn:
                    conn.execute("BEGIN IMMEDIATE")
                    if conn.execute("UPDATE rule_validation_control SET blocked=1,reason='uncertain_process',updated_at=?,last_job_id=? WHERE id=1", (time.time(), job_id)).rowcount != 1:
                        raise RuntimeError("测试安全状态不可用")
            if (not isinstance(result, GoldCheckResult) or result.category != snapshot.category
                    or result.candidate_sha256 != digest):
                raise ValueError("测试结果与候选规则不匹配")
            if result.passed and (result.exit_code != 0 or result.collected_tests < 1
                                  or result.passed_tests != result.collected_tests
                                  or result.skipped_tests or result.failed_test_ids):
                raise ValueError("测试结果不完整")
            lease_expired = False
            with closing(self.accounts._connect()) as conn, conn:
                conn.execute("BEGIN IMMEDIATE")
                actor, current = self._candidate(conn, token, candidate_id, current=True)
                job = conn.execute("SELECT * FROM rule_validation_jobs WHERE id=?", (job_id,)).fetchone()
                if (actor["id"] != actor_id or current["sha256"] != digest or current["base_generation"] != generation
                        or job is None or job["status"] != "running"):
                    raise ValueError("候选规则、账户或测试任务已变化")
                now = time.time()
                if job["lease_until"] <= now:
                    # 收尾时发现本任务租约已过期：同一事务内先原子落闸再把任务置为 abandoned，
                    # 保证守卫阻断新任务、审核通过和发布，直至离线检查恢复
                    self._arm_control(conn, "expired_job", job_id, now)
                    conn.execute("UPDATE rule_validation_jobs SET status='abandoned' WHERE id=? AND status='running'", (job_id,))
                    lease_expired = True
                else:
                    result_id = uuid.uuid4().hex
                    conn.execute("INSERT INTO rule_validation_results VALUES (?,?,?,?,?,?,?)", (
                        result_id, candidate_id, digest, generation, actor_id, now,
                        json.dumps(asdict(result), ensure_ascii=False, sort_keys=True),
                    ))
                    conn.execute("UPDATE rule_validation_jobs SET status='completed', result_id=? WHERE id=?", (result_id, job_id))
                    self.accounts._audit(conn, actor_id, "validate_rule_candidate", candidate_id)
                    completed = {"id": result_id, "candidate_id": candidate_id, "candidate_sha256": digest,
                                 "base_generation": generation, "actor_id": actor_id, "created_at": now,
                                 **asdict(result)}
            if lease_expired:
                raise RuntimeError("测试任务租约已过期，请检查进程与工作区后再恢复")
            return completed
        except Exception:
            if job_id is not None:
                with closing(self.accounts._connect()) as conn, conn:
                    conn.execute("BEGIN IMMEDIATE")
                    if unsafe or process_control_uncertain():
                        if conn.execute("UPDATE rule_validation_control SET blocked=1,reason='uncertain_process',updated_at=?,last_job_id=? WHERE id=1", (time.time(), job_id)).rowcount != 1:
                            raise RuntimeError("测试安全状态不可用")
                    job = conn.execute("SELECT status, lease_until FROM rule_validation_jobs WHERE id=?", (job_id,)).fetchone()
                    if job is not None and job["status"] == "running" and job["lease_until"] <= time.time():
                        # 异常收尾时租约已过期：同样先落闸再置 abandoned；落闸失败则整事务回滚保留 running
                        self._arm_control(conn, "expired_job", job_id, time.time())
                        conn.execute("UPDATE rule_validation_jobs SET status='abandoned' WHERE id=? AND status='running'", (job_id,))
                    else:
                        conn.execute("UPDATE rule_validation_jobs SET status='failed' WHERE id=? AND status='running'", (job_id,))
            raise
        finally:
            _VALIDATION_SLOTS.release()

    def list_results(self, token: str, candidate_id: str) -> list[dict[str, Any]]:
        with closing(self.accounts._connect()) as conn:
            conn.execute("BEGIN")
            self._candidate(conn, token, candidate_id, current=False)
            rows = conn.execute("SELECT * FROM rule_validation_results WHERE candidate_id=? ORDER BY rowid DESC",
                                (candidate_id,)).fetchall()
            return [{"id": row["id"], "candidate_id": row["candidate_id"],
                     "candidate_sha256": row["candidate_sha256"], "base_generation": row["base_generation"],
                     "actor_id": row["actor_id"], "created_at": row["created_at"],
                     **json.loads(row["result_json"])} for row in rows]
