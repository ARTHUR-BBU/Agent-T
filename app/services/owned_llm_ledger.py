"""Private account-job call ledger; not wired to any network transport yet.

Pending records represent authorization to try, not proof of sending or billing.
This ledger never stores prompts, output, credentials, or provider error bodies.
"""
from __future__ import annotations

import time
import uuid
from contextlib import closing

from app.services.account_store import AuthorizationError
from app.services.owned_review_jobs import OwnedReviewJobs

_SCHEMA = """CREATE TABLE owned_llm_attempts (
    id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES owned_review_jobs(id) ON DELETE CASCADE,
    node TEXT NOT NULL, provider TEXT NOT NULL, model TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK(outcome IN ('pending','success','timeout','provider_error','unknown')),
    started_at REAL NOT NULL, finished_at REAL,
    input_tokens INTEGER, output_tokens INTEGER,
    business_outcome TEXT CHECK(business_outcome IN ('success','parse_failed')), business_finished_at REAL
)"""
_NODES = frozenset({"precheck", "category_suggest", "model_review", "quality", "objection", "ask", "revision", "impact", "remediate"})
_PROVIDERS = frozenset({"deepseek", "zhipu", "xai", "cloudflare", "typesafe"})
_MODELS = {"deepseek": frozenset({"deepseek-v4-flash"}), "zhipu": frozenset({"glm-5.2"}),
           "xai": frozenset({"grok-2-latest"}), "cloudflare": frozenset({"clef-flash"}), "typesafe": frozenset()}
_TERMINAL = frozenset({"success", "timeout", "provider_error"})


class OwnedLLMLedger:
    def __init__(self, jobs: OwnedReviewJobs, *,
                 data_integrity_check: bool = True) -> None:
        # data_integrity_check=False 仅限维护恢复路径（owned_review_maintenance）：
        # 孤儿 pending 行（任务行已消失）正是恢复要收口的对象——数据行级
        # foreign_key_check 硬门槛会让恢复进不去（codex 审核 P1-1：孤儿
        # 记录在进入修复逻辑之前就挡住恢复）。结构 DDL 校验不放宽。
        self.jobs = jobs
        with closing(jobs.accounts._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='owned_llm_attempts'").fetchone()
            if existing is None:
                conn.execute(_SCHEMA)
                existing = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='owned_llm_attempts'").fetchone()
            if (not existing or " ".join(existing["sql"].split()).lower() != " ".join(_SCHEMA.split()).lower()
                    or (data_integrity_check and conn.execute(
                        "PRAGMA foreign_key_check(owned_llm_attempts)").fetchone() is not None)):
                raise RuntimeError("账户调用账本结构无效，需要离线迁移")

    def begin(self, token: str, job_id: str, worker_secret: str, *, node: str, provider: str, model: str) -> str:
        if (not isinstance(node, str) or node not in _NODES or not isinstance(provider, str) or provider not in _PROVIDERS
                or not isinstance(model, str) or model not in _MODELS[provider]):
            raise ValueError("调用信息无效")
        call_id = uuid.uuid4().hex
        with closing(self.jobs.accounts._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            self.jobs._authorize_outbound_in_transaction(conn, token, job_id, worker_secret)
            conn.execute("INSERT INTO owned_llm_attempts (id,job_id,node,provider,model,outcome,started_at) "
                         "VALUES (?,?,?,?,?,'pending',?)", (call_id, job_id, node, provider, model, time.time()))
            self.jobs._authorize_outbound_in_transaction(conn, token, job_id, worker_secret)
        return call_id

    def settle(self, token: str, job_id: str, worker_secret: str, call_id: str, *, outcome: str,
               input_tokens: int | None = None, output_tokens: int | None = None) -> None:
        if (not isinstance(outcome, str) or outcome not in _TERMINAL or any(value is not None and (type(value) is not int or not 0 <= value <= 100_000_000)
                                            for value in (input_tokens, output_tokens))):
            raise ValueError("调用结果无效")
        with closing(self.jobs.accounts._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            self.jobs._authorize_outbound_in_transaction(conn, token, job_id, worker_secret)
            changed = conn.execute("UPDATE owned_llm_attempts SET outcome=?,finished_at=?,input_tokens=?,output_tokens=? "
                                   "WHERE id=? AND job_id=? AND outcome='pending'",
                                   (outcome, time.time(), input_tokens, output_tokens, call_id, job_id)).rowcount
            if changed != 1:
                raise AuthorizationError("调用记录不可用")
            self.jobs._authorize_outbound_in_transaction(conn, token, job_id, worker_secret)

    def reconcile(self, *, limit: int = 100) -> int:
        """Mark abandoned attempts unknown; never infer whether sending occurred.

        B7-交接-20261110（坏记录隔离）：JOIN 不到的孤儿 pending 行（任务行
        或审查行已消失）此前永远不会入选——补一段孤儿 UPDATE，同一 unknown
        语义收口；仍然不做「是否已发送」的推断。"""
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("清理数量无效")
        with closing(self.jobs.accounts._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            now = time.time()
            ttl = self.jobs.owned.reviews.ttl_seconds
            cutoff = now - ttl if ttl else None
            # codex 三轮 P2-3 + 五轮 P2-2 + 六轮 P1：公平必须在 **SQL 截断
            # 之前**保证——此前两路查询按 started_at 排序加 LIMIT，SQLite 把
            # 数值排在文本之前，同分支正常记录足够多时坏时间行根本进不了
            # Python 候选集，预留名额形同虚设（六轮复现）。现在三路独立
            # 选取，各自不受其他类别干扰：
            # ①坏时间候选（typeof 非 real，两分支合并）独立预留份额
            #   （1/4 窗口至少 1）——损伤存量有限，优先清走；
            # ②数值孤儿与 ③数值普通失效各自 LIMIT 剩余窗口，合并按时间
            #   全局排序截取——小窗口双向公平。
            _reserved_share = max(1, (limit + 3) // 4)
            _HEALTHY_JOIN = ("SELECT 1 FROM owned_review_jobs j JOIN reviews r "
                             "ON r.id=j.review_id AND r.owner_user_id=j.owner_user_id "
                             "WHERE j.id=a.job_id")
            _STALE_COND = ("(j2.status!='running' OR j2.lease_until<=? OR (? IS NOT NULL AND r2.created_at<=?) "
                           "OR NOT EXISTS (SELECT 1 FROM account_sessions s JOIN account_users u ON u.id=s.user_id "
                           "WHERE s.token_hash=j2.session_hash AND s.user_id=j2.owner_user_id AND s.expires_at>? "
                           "AND u.active=1 AND u.must_change_password=0))")
            bad_rows = conn.execute(
                "SELECT a.id,a.rowid FROM owned_llm_attempts a WHERE a.outcome='pending' "
                "AND typeof(a.started_at) NOT IN ('integer','real') AND ("
                "NOT EXISTS (" + _HEALTHY_JOIN + ") OR EXISTS ("
                "SELECT 1 FROM owned_review_jobs j2 JOIN reviews r2 "
                "ON r2.id=j2.review_id AND r2.owner_user_id=j2.owner_user_id "
                "WHERE j2.id=a.job_id AND " + _STALE_COND + ")) "
                "ORDER BY a.rowid LIMIT ?", (now, cutoff, cutoff, now, _reserved_share)).fetchall()
            take_bad = bad_rows[:_reserved_share]
            remaining = limit - len(take_bad)
            marked = 0
            for r in take_bad:
                conn.execute("UPDATE owned_llm_attempts SET outcome='unknown',finished_at=? WHERE id=? AND outcome='pending'",
                             (now, r["id"]))
                marked += 1
            if remaining > 0:
                numeric_orphans = conn.execute(
                    "SELECT a.id,a.started_at FROM owned_llm_attempts a WHERE a.outcome='pending' "
                    "AND typeof(a.started_at) IN ('integer','real') AND NOT EXISTS ("
                    + _HEALTHY_JOIN + ") ORDER BY a.started_at,a.id LIMIT ?", (remaining,)).fetchall()
                numeric_normal = conn.execute(
                    "SELECT a.id,a.started_at FROM owned_llm_attempts a JOIN owned_review_jobs j ON j.id=a.job_id "
                    "JOIN reviews r ON r.id=j.review_id AND r.owner_user_id=j.owner_user_id "
                    "WHERE a.outcome='pending' AND typeof(a.started_at) IN ('integer','real') AND (j.status!='running' "
                    "OR j.lease_until<=? OR (? IS NOT NULL AND r.created_at<=?) OR NOT EXISTS (SELECT 1 FROM account_sessions s "
                    "JOIN account_users u ON u.id=s.user_id WHERE s.token_hash=j.session_hash "
                    "AND s.user_id=j.owner_user_id AND s.expires_at>? AND u.active=1 AND u.must_change_password=0)) "
                    "ORDER BY a.started_at,a.id LIMIT ?", (now, cutoff, cutoff, now, remaining)).fetchall()
                numeric = sorted([(r["started_at"], r["id"]) for r in numeric_orphans + numeric_normal])
                for _, call_id in numeric[:remaining]:
                    conn.execute("UPDATE owned_llm_attempts SET outcome='unknown',finished_at=? WHERE id=? AND outcome='pending'",
                                 (now, call_id))
                    marked += 1
            return marked

    def settle_business(self, token: str, job_id: str, worker_secret: str, call_id: str, *, outcome: str) -> None:
        """Record a parsed decision separately from transport success; no output is stored."""
        if outcome not in ("success", "parse_failed"):
            raise ValueError("业务结算结果无效")
        with closing(self.jobs.accounts._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            self.jobs._authorize_outbound_in_transaction(conn, token, job_id, worker_secret)
            changed = conn.execute("UPDATE owned_llm_attempts SET business_outcome=?,business_finished_at=? "
                                   "WHERE id=? AND job_id=? AND node='category_suggest' AND outcome='success' "
                                   "AND business_outcome IS NULL", (outcome, time.time(), call_id, job_id)).rowcount
            if changed != 1:
                raise AuthorizationError("调用记录不可用")
            self.jobs._authorize_outbound_in_transaction(conn, token, job_id, worker_secret)
