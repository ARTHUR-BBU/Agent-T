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


# codex 五轮 P1：保留 3a57bad 已验证的历史建库文本**逐字原文**——
# 迁移结构检查（_check_schema）逐字比较 sqlite_master.sql，运行时建库
# 与迁移参照库必须产出字节一致的 DDL（空白改写会让合法旧库被误报
# 「结构不受支持」）。原子性（四轮 P1-1）由 _split_sqlite_script +
# 同一 BEGIN IMMEDIATE 事务逐条执行保证（executescript 会隐式提交不能用）。
_VALIDATION_SCHEMA_SQL = """
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
            """


# 施工卡-20261114-2（codex 实测）：ALTER TABLE ADD COLUMN 会**改写 sqlite_master
# 中保存的建表 SQL**（在列清单末尾拼接新列定义），逐字节迁移比对随之破坏——
# 因此恢复代次**不给任何既有表加列**，改走独立追加式账本：
# - rule_validation_recovery_log：维护窗口/解锁/拒绝尝试/风险接受登记；
#   当前恢复代次 = 其中 action='unlock' 的行数（追加触发器 + 写锁串行 ⇒ 单调）；
# - rule_evidence_generations：金标/完整回归/审核三类证据行的代次盖章，
#   写入方在各自写事务内随证据行一起插入（evidence_id 对应各证据表主键；
#   三类分属不同表，故不设单一外键，一致性由写入方事务保证）。
# - rule_validation_recovery_marker：账本启用标记（codex P1-20261115）——
#   与账本两表同一事务建立；**账本在而标记不在 = 结构不可信，标记在而账本
#   不在 = 账本丢失**，一律 fail-closed 拒绝、绝不静默补空（否则代次归零、
#   未盖章旧证据会被误当恢复前证据）。诚实边界：标记表与账本**连同标记
#   一起被有权限者彻底删除**时，结构与未升级旧库无法区分，按旧库升级
#   处理——此类彻底破坏属于离线处置边界（需保留安全闸状态/任务与测试
#   历史的离线重建），程序侧无法防御，不在自动恢复范围。
# SQLite TEXT PRIMARY KEY 仍可接受 NULL（codex 独立验证），主键列显式
# NOT NULL；代次以 typeof 强制整数（codex 数据约束建议）。
_RECOVERY_SCHEMA_SQL = """
                CREATE TABLE IF NOT EXISTS rule_validation_recovery_log (
                    id TEXT NOT NULL PRIMARY KEY,
                    action TEXT NOT NULL CHECK(action IN ('window_open','window_close','unlock','rejected_attempt','residual_risk_accepted')),
                    actor_id TEXT NOT NULL REFERENCES account_users(id),
                    incident_reason TEXT, last_job_id TEXT,
                    evidence_json TEXT NOT NULL, db_file_identity TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS rule_evidence_generations (
                    evidence_id TEXT NOT NULL PRIMARY KEY,
                    evidence_kind TEXT NOT NULL CHECK(evidence_kind IN ('validation','full_regression','review')),
                    recovery_generation INTEGER NOT NULL CHECK(typeof(recovery_generation)='integer' AND recovery_generation >= 1),
                    created_at REAL NOT NULL
                );
                CREATE TRIGGER IF NOT EXISTS recovery_log_no_update BEFORE UPDATE ON rule_validation_recovery_log
                BEGIN SELECT RAISE(ABORT, 'recovery log is append-only'); END;
                CREATE TRIGGER IF NOT EXISTS recovery_log_no_delete BEFORE DELETE ON rule_validation_recovery_log
                BEGIN SELECT RAISE(ABORT, 'recovery log is append-only'); END;
                CREATE TRIGGER IF NOT EXISTS recovery_log_no_replace BEFORE INSERT ON rule_validation_recovery_log
                WHEN EXISTS (SELECT 1 FROM rule_validation_recovery_log WHERE id=NEW.id)
                BEGIN SELECT RAISE(ABORT, 'recovery log is append-only'); END;
                CREATE TRIGGER IF NOT EXISTS evidence_generation_no_update BEFORE UPDATE ON rule_evidence_generations
                BEGIN SELECT RAISE(ABORT, 'evidence generations are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS evidence_generation_no_delete BEFORE DELETE ON rule_evidence_generations
                BEGIN SELECT RAISE(ABORT, 'evidence generations are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS evidence_generation_no_replace BEFORE INSERT ON rule_evidence_generations
                WHEN EXISTS (SELECT 1 FROM rule_evidence_generations WHERE evidence_id=NEW.evidence_id)
                BEGIN SELECT RAISE(ABORT, 'evidence generations are append-only'); END;
                CREATE TABLE IF NOT EXISTS rule_validation_recovery_marker (
                    id INTEGER PRIMARY KEY CHECK(id=1), initialized_at REAL NOT NULL
                );
                CREATE TRIGGER IF NOT EXISTS recovery_marker_no_delete BEFORE DELETE ON rule_validation_recovery_marker
                BEGIN SELECT RAISE(ABORT, 'recovery marker is retained'); END;
                CREATE TRIGGER IF NOT EXISTS recovery_marker_no_update BEFORE UPDATE ON rule_validation_recovery_marker
                BEGIN SELECT RAISE(ABORT, 'recovery marker is retained'); END;
            """
# 账本域触发器 → 所属表（初始化一致性检查用；触发器被删时拒绝而非静默重建）
_RECOVERY_EXPECTED_TRIGGERS = {
    "recovery_log_no_update": "rule_validation_recovery_log",
    "recovery_log_no_delete": "rule_validation_recovery_log",
    "recovery_log_no_replace": "rule_validation_recovery_log",
    "evidence_generation_no_update": "rule_evidence_generations",
    "evidence_generation_no_delete": "rule_evidence_generations",
    "evidence_generation_no_replace": "rule_evidence_generations",
    "recovery_marker_no_delete": "rule_validation_recovery_marker",
    "recovery_marker_no_update": "rule_validation_recovery_marker",
}


def _split_sqlite_script(script: str) -> list[str]:
    """按 sqlite3.complete_statement 拆分建库脚本（正确处理触发器
    BEGIN...END 内嵌分号）；用于在同一写事务内逐条执行。"""
    statements: list[str] = []
    buffer = ""
    for line in script.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            text = buffer.strip()
            if text:
                statements.append(text)
            buffer = ""
    if buffer.strip():
        statements.append(buffer.strip())
    return statements



class RuleValidationService:
    def __init__(self, accounts: AccountStore) -> None:
        self.accounts = accounts
        self.submissions = RuleSubmissionService(accounts)
        with closing(accounts._connect()) as conn, conn:
            # codex 三轮 P1-1 + 四轮 P1-1：初始检查、建表、控制行建立必须在
            # **同一个写事务**内（BEGIN IMMEDIATE 串行化并发构造）——此前
            # executescript 隐式提交拆散了事务：并发构造者 A 建完表未插控制行
            # 时，B 看到「表齐全但控制行缺失」插入 blocked=1，新库被永久误阻断。
            # executescript 会隐式提交不能包事务，改为逐条 execute（_VALIDATION_DDL）。
            # 损坏库（三表部分缺失）仍 fail-closed 拒绝，绝不把安全闸补成可用。
            conn.execute("BEGIN IMMEDIATE")
            _names = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
                "('rule_validation_control','rule_validation_jobs','rule_validation_results')")}
            if _names and len(_names) < 3:
                raise RuntimeError("规则验证结构不完整（控制/任务/结果表部分缺失），需要离线迁移")
            _had_control = "rule_validation_control" in _names
            for _ddl in _split_sqlite_script(_VALIDATION_SCHEMA_SQL):
                conn.execute(_ddl)
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(rule_validation_control)")}
            if "last_job_id" not in columns:
                conn.execute("ALTER TABLE rule_validation_control ADD COLUMN last_job_id TEXT")
            # codex 三轮 P1-1：默认控制行只对**真新库**插 blocked=0；已存在的
            # 控制表若控制行缺失（数据损伤）→ 防御性插入 blocked=1（保持阻断，
            # 不能自动补成可用）。防在线复位触发器只拦 UPDATE，拦不住这条
            # INSERT——所以必须在这里显式守门。
            if _had_control:
                _row = conn.execute("SELECT blocked FROM rule_validation_control WHERE id=1").fetchone()
                if _row is None:
                    conn.execute("INSERT INTO rule_validation_control(id,blocked,reason,updated_at) "
                                 "VALUES (1,1,'控制行缺失，防御阻断，需离线核查',?)", (time.time(),))
            else:
                conn.execute("INSERT OR IGNORE INTO rule_validation_control(id,blocked,reason,updated_at) "
                             "VALUES (1,0,'',0)")
            # 恢复账本（施工卡-20261114-2 + codex P1-20261115）：fail-closed
            # 一致性检查——账本缺失/不完整/与标记不一致时拒绝初始化，绝不静默
            # 补空（补空 = 代次归零 = 未盖章旧证据被误当恢复前证据）
            _recovery_tables_present = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
                "('rule_validation_recovery_log','rule_evidence_generations',"
                "'rule_validation_recovery_marker')")}
            _ledger_present = _recovery_tables_present & {"rule_validation_recovery_log", "rule_evidence_generations"}
            _marker_present = "rule_validation_recovery_marker" in _recovery_tables_present
            if len(_ledger_present) == 1:
                raise RuntimeError("恢复账本不完整（两表中缺一），需要离线核查")
            if _marker_present != bool(_ledger_present):
                # 标记在而账本丢 = 启用后结构丢失；账本在而标记丢 = 结构不可信
                raise RuntimeError("恢复账本与初始化标记不一致，需要离线核查")
            if _ledger_present:
                _marker_row = conn.execute(
                    "SELECT initialized_at FROM rule_validation_recovery_marker WHERE id=1").fetchone()
                if _marker_row is None:
                    raise RuntimeError("恢复初始化标记行缺失，需要离线核查")
                _existing_triggers = {row[0]: row[1] for row in conn.execute(
                    "SELECT name,tbl_name FROM sqlite_master WHERE type='trigger'")}
                _missing = sorted(name for name, table in _RECOVERY_EXPECTED_TRIGGERS.items()
                                  if _existing_triggers.get(name) != table)
                if _missing:
                    # 触发器被删：如实拒绝，不自动补齐（补齐会被当成「历史未被修改」）
                    raise RuntimeError("恢复账本触发器缺失（%s），需要离线核查" % ",".join(_missing))
            else:
                # 全新库或未升级旧库：同一写事务内建账本两表+标记表并落标记行；
                # 既有对象逐字节不动
                for _ddl in _split_sqlite_script(_RECOVERY_SCHEMA_SQL):
                    conn.execute(_ddl)
                conn.execute("INSERT INTO rule_validation_recovery_marker(id,initialized_at) "
                             "VALUES (1,?)", (time.time(),))

    def unlock_generation(self, conn: sqlite3.Connection) -> int:
        """当前恢复代次 = 恢复账本中成功解锁行数（施工卡-20261114-2/3）。

        追加式触发器 + 写锁串行保证单调；0 表示从未离线恢复过——
        此时未盖章的存量证据即当前代次证据。"""
        return conn.execute(
            "SELECT COUNT(*) FROM rule_validation_recovery_log WHERE action='unlock'").fetchone()[0]

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
                    # 施工卡-20261114-2：INSERT 一律写明确列名，不再依赖列序
                    conn.execute("INSERT INTO rule_validation_jobs "
                                 "(id,candidate_id,actor_id,status,started_at,lease_until,result_id) "
                                 "VALUES (?,?,?,?,?,?,?)", (
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
                    # job 行存在 ⇒ job_id 必为非空（上方已拒绝 job is None）
                    self._arm_control(conn, "expired_job", str(job_id), now)
                    conn.execute("UPDATE rule_validation_jobs SET status='abandoned' WHERE id=? AND status='running'", (job_id,))
                    lease_expired = True
                else:
                    result_id = uuid.uuid4().hex
                    conn.execute("INSERT INTO rule_validation_results "
                                 "(id,candidate_id,candidate_sha256,base_generation,actor_id,created_at,result_json) "
                                 "VALUES (?,?,?,?,?,?,?)", (
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
