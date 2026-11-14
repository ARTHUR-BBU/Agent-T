"""Account-bound review job transactions; not yet wired to a worker or HTTP API.

The caller must enforce CSRF and protect the in-memory session/job secrets. This
module does not authorize model calls or protect the existing shared routes.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import time
import uuid
from contextlib import closing
from typing import Any

from app.services.account_store import AuthorizationError, _token_digest
from app.services.owned_review_store import OwnedReviewStore

logger = logging.getLogger(__name__)


class _QuarantinableRecord(Exception):
    """可明确识别的坏记录（codex 审核 P2）：只隔离自身，不影响整批。

    子类语义：orphan=审查行缺失（任务行是死行，删除以消除 FK 违例）；
    corrupt=审查 data 不可解析；mismatch=审查状态与任务行矛盾。"""


class _OrphanJob(_QuarantinableRecord):
    pass


class _CorruptRecord(_QuarantinableRecord):
    pass


class _StatusMismatch(_QuarantinableRecord):
    pass


class _OwnershipMismatch(_QuarantinableRecord):
    pass


# 孤儿任务删除前必须核对的级联子表（全部 REFERENCES owned_review_jobs
# ON DELETE CASCADE）——codex 三轮 P2-4：预审声明/结果与 worker 声明
# 同样是要保留的数据（分类/选择/预算/摘要），「没有调用账本」不等于
# 「没有任何需要保留的信息」
_ORPHAN_CHILD_TABLES = ("owned_llm_attempts", "owned_review_precheck_claims",
                        "owned_review_precheck_results", "owned_review_worker_claims")
_ORPHAN_CHILD_QUOTED = {name: '"' + name + '"' for name in _ORPHAN_CHILD_TABLES}


_RESULT_FIELDS = frozenset({"items", "scorecard", "text", "text_preview", "policies", "completion", "error",
                            "document_version", "clause_index", "facts", "verify", "quality", "objections",
                            "blind_candidates", "blind_skipped_messages", "blind_skipped_reason", "blind_enabled",
                            "rule_pack", "evidence_index"})
_JOB_SCHEMA = """CREATE TABLE owned_review_jobs (
                id TEXT PRIMARY KEY, review_id TEXT NOT NULL UNIQUE REFERENCES reviews(id) ON DELETE CASCADE,
                owner_user_id TEXT NOT NULL REFERENCES account_users(id),
                session_hash TEXT NOT NULL, worker_hash TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('running','done','failed')),
                created_at REAL NOT NULL, lease_until REAL NOT NULL
            )"""


class OwnedReviewJobs:
    """Bind new reviews and jobs to the authenticated session in one database."""

    def __init__(self, owned: OwnedReviewStore, *,
                 data_integrity_check: bool = True) -> None:
        # data_integrity_check=False 仅限维护恢复路径（owned_review_maintenance）：
        # 数据行级 foreign_key_check 的违例正是恢复要隔离的对象——硬门槛会
        # 让恢复在最需要时跑不起来（handoff-20261110-bad-record-isolation-before）。
        # 结构校验（DDL/列集/级联）不放宽，任何调用方都严格把关。
        self.owned = owned
        self.accounts = owned.accounts
        with closing(self.accounts._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(reviews)")}
            if not {"id", "created_at", "data", "owner_user_id"}.issubset(columns):
                raise RuntimeError("账户合同数据库尚未初始化")
            existing = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='owned_review_jobs'").fetchone()
            if existing is None:
                conn.execute(_JOB_SCHEMA)
                existing = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='owned_review_jobs'").fetchone()
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(owned_review_jobs)")}
            foreign_keys = conn.execute("PRAGMA foreign_key_list(owned_review_jobs)").fetchall()
            if (not existing or " ".join(existing["sql"].split()).lower() != " ".join(_JOB_SCHEMA.split()).lower()
                    or columns != {"id", "review_id", "owner_user_id", "session_hash", "worker_hash", "status", "created_at", "lease_until"}
                    or not any(row["table"] == "reviews" and row["from"] == "review_id" and row["to"] == "id"
                               and row["on_delete"] == "CASCADE" for row in foreign_keys)
                    or (data_integrity_check and conn.execute(
                        "PRAGMA foreign_key_check(owned_review_jobs)").fetchone() is not None)):
                raise RuntimeError("账户审查任务表结构无效，需要离线迁移")

    def create(self, token: str, *, filename: str, category: str, lease_seconds: int = 300,
               raw: bytes | None = None, declared_stance: str = "neutral", input_kind: str = "file",
               require_precheck: bool | None = None) -> tuple[str, str, str]:
        if require_precheck is None:
            require_precheck = raw is not None
        if (not isinstance(filename, str) or not 1 <= len(filename) <= 255 or
                not isinstance(category, str) or not 1 <= len(category) <= 64 or
                type(lease_seconds) is not int or not 1 <= lease_seconds <= 900):
            raise ValueError("审查任务参数无效")
        if (raw is not None and (not isinstance(raw, bytes) or not raw or len(raw) > 10 * 1024 * 1024)
                or not isinstance(declared_stance, str) or not 1 <= len(declared_stance) <= 64
                or input_kind not in ("file", "pasted_text") or type(require_precheck) is not bool
                or (require_precheck and raw is None)):
            raise ValueError("合同内容或立场无效")
        rid, job_id, worker_secret = uuid.uuid4().hex[:12], uuid.uuid4().hex, secrets.token_urlsafe(32)
        data = json.dumps({"id": rid, "filename": filename, "category": category, "status": "processing",
                           **({"input_sha256": hashlib.sha256(raw).hexdigest(), "input_stance": declared_stance,
                               "input_kind": input_kind, "require_precheck": require_precheck}
                              if raw is not None else {})}, ensure_ascii=False)
        with closing(self.accounts._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            actor = self.accounts._session(conn, token)
            now = time.time()
            conn.execute("INSERT INTO reviews (id,created_at,data,owner_user_id) VALUES (?,?,?,?)", (rid, now, data, actor["id"]))
            conn.execute("INSERT INTO owned_review_jobs VALUES (?,?,?,?,?,?,?,?)", (
                job_id, rid, actor["id"], _token_digest(token), hashlib.sha256(worker_secret.encode()).hexdigest(),
                "running", now, now + lease_seconds,
            ))
            self.accounts._session(conn, token)
        return rid, job_id, worker_secret

    def authorize_outbound(self, token: str, job_id: str, worker_secret: str) -> str:
        """Check at call time; a later revocation cannot retract already sent data."""
        with closing(self.accounts._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            return self._authorize_outbound_in_transaction(conn, token, job_id, worker_secret)

    def _authorize_outbound_in_transaction(self, conn: Any, token: str, job_id: str, worker_secret: str) -> str:
        if not isinstance(worker_secret, str) or not 1 <= len(worker_secret) <= 128:
            raise AuthorizationError("审查任务不可用")
        worker_hash = hashlib.sha256(worker_secret.encode()).hexdigest()
        actor = self.accounts._session(conn, token)
        row = conn.execute(
            "SELECT j.*,r.created_at AS review_created_at,r.data AS review_data "
            "FROM owned_review_jobs j JOIN reviews r ON r.id=j.review_id AND r.owner_user_id=j.owner_user_id "
            "WHERE j.id=?", (job_id,),
        ).fetchone()
        if (row is None or row["owner_user_id"] != actor["id"] or row["session_hash"] != _token_digest(token)
                or not hmac.compare_digest(row["worker_hash"], worker_hash) or row["status"] != "running"
                or row["lease_until"] <= time.time()):
            raise AuthorizationError("审查任务不可用")
        ttl = self.owned.reviews.ttl_seconds
        if ttl and row["review_created_at"] + ttl <= time.time():
            raise AuthorizationError("审查任务不可用")
        data = json.loads(row["review_data"])
        if data.get("id") != row["review_id"] or data.get("status") != "processing":
            raise AuthorizationError("审查任务不可用")
        self.accounts._session(conn, token)
        if row["lease_until"] <= time.time() or (ttl and row["review_created_at"] + ttl <= time.time()):
            raise AuthorizationError("审查任务不可用")
        return row["review_id"]

    def commit(self, token: str, job_id: str, worker_secret: str, *, result: dict[str, Any], failed: bool = False) -> str:
        if not isinstance(result, dict) or not result.keys() <= _RESULT_FIELDS or type(failed) is not bool:
            raise ValueError("审查结果无效")
        if not isinstance(worker_secret, str) or not 1 <= len(worker_secret) <= 128:
            raise AuthorizationError("审查任务不可用")
        worker_hash = hashlib.sha256(worker_secret.encode()).hexdigest()
        with closing(self.accounts._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            actor = self.accounts._session(conn, token)
            job = conn.execute("SELECT * FROM owned_review_jobs WHERE id=?", (job_id,)).fetchone()
            if (job is None or job["owner_user_id"] != actor["id"] or job["session_hash"] != _token_digest(token)
                    or not hmac.compare_digest(job["worker_hash"], worker_hash) or job["status"] != "running"
                    or job["lease_until"] <= time.time()):
                raise AuthorizationError("审查任务不可用")
            row = conn.execute("SELECT created_at,data FROM reviews WHERE id=? AND owner_user_id=?", (job["review_id"], actor["id"])).fetchone()
            if row is None:
                raise AuthorizationError("审查任务不可用")
            ttl = self.owned.reviews.ttl_seconds
            if ttl and row["created_at"] + ttl <= time.time():
                raise AuthorizationError("审查任务不可用")
            data = json.loads(row["data"])
            if data.get("id") != job["review_id"] or data.get("status") != "processing":
                raise ValueError("审查记录状态无效")
            data.update(result)
            data["status"] = "error" if failed else "done"
            self.accounts._session(conn, token)
            if job["lease_until"] <= time.time() or (ttl and row["created_at"] + ttl <= time.time()):
                raise AuthorizationError("审查任务不可用")
            conn.execute("UPDATE reviews SET data=? WHERE id=? AND owner_user_id=?", (json.dumps(data, ensure_ascii=False), job["review_id"], actor["id"]))
            conn.execute("UPDATE owned_review_jobs SET status=? WHERE id=? AND status='running'", ("failed" if failed else "done", job_id))
            return job["review_id"]

    def reap_stale(self, *, limit: int = 100) -> int:
        """Internal maintenance: invalidate expired leases or revoked sessions atomically.

        B7-交接-20261110（坏记录隔离）：单条记录解析/归属失败时用 SAVEPOINT
        只回滚该条并**隔离任务行**（status='failed'，不再入选窗口）——修复
        「一条坏记录全批回滚且每轮重新入选、健康记录结构性饿死」的漏洞
        （复现见 docs/handoff-20261110-bad-record-isolation-before.md）。
        隔离动作不推断外发结果（账本 reconcile 的 unknown 语义不变）；
        无法解析的审查行绝不猜测改写，留给离线迁移。返回清理条数。"""
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("清理数量无效")
        with closing(self.accounts._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            now = time.time()
            ttl = self.owned.reviews.ttl_seconds
            cutoff = now - ttl if ttl else None
            jobs = conn.execute("SELECT j.* FROM owned_review_jobs j WHERE j.status='running' AND "
                                "(j.lease_until<=? OR (? IS NOT NULL AND EXISTS (SELECT 1 FROM reviews r "
                                "WHERE r.id=j.review_id AND r.owner_user_id=j.owner_user_id AND r.created_at<=?)) "
                                "OR NOT EXISTS (SELECT 1 FROM account_sessions s "
                                "JOIN account_users u ON u.id=s.user_id WHERE s.token_hash=j.session_hash "
                                "AND s.user_id=j.owner_user_id AND s.expires_at>? AND u.active=1 "
                                "AND u.must_change_password=0)) ORDER BY j.created_at,j.id LIMIT ?", (now, cutoff, cutoff, now, limit)).fetchall()
            quarantined = 0
            for job in jobs:
                conn.execute("SAVEPOINT reap_one")
                try:
                    # codex 二轮 P1-1：存在性（不带 owner）与归属分开判——
                    # 「审查行缺失」和「审查存在但归属他人」是两种坏记录，
                    # 处置不同（见下方 except 分支），混判会级联删账本
                    row = conn.execute("SELECT created_at,data,owner_user_id FROM reviews WHERE id=?",
                                       (job["review_id"],)).fetchone()
                    if row is None:
                        raise _OrphanJob()
                    if row["owner_user_id"] != job["owner_user_id"]:
                        raise _OwnershipMismatch()
                    if ttl and row["created_at"] + ttl <= now:
                        self.owned._remove_expired(conn, job["review_id"])
                        conn.execute("RELEASE SAVEPOINT reap_one")
                        continue
                    try:
                        data = json.loads(row["data"])
                    except (ValueError, TypeError):
                        # 损坏行不可解析：绝不猜测改写审查内容——只隔离任务行
                        # 停止占窗，审查行原样留给离线迁移
                        raise _CorruptRecord()
                    # codex 二轮 P1-2：null/[] 等合法但非对象的 JSON 也会在
                    # .get() 上炸整批——显式识别为坏记录（保留原文、隔离任务）
                    if not isinstance(data, dict):
                        raise _CorruptRecord()
                    if data.get("id") != job["review_id"] or data.get("status") != "processing":
                        raise _StatusMismatch()
                    data.update(status="error", error="审查任务已中断，请重新提交")
                    conn.execute("UPDATE reviews SET data=? WHERE id=? AND owner_user_id=?",
                                 (json.dumps(data, ensure_ascii=False), job["review_id"], job["owner_user_id"]))
                    conn.execute("UPDATE owned_review_jobs SET status='failed' WHERE id=? AND status='running'", (job["id"],))
                    conn.execute("RELEASE SAVEPOINT reap_one")
                except _QuarantinableRecord as exc:
                    # 单条隔离：回滚本条改动后处置任务行。
                    # codex 二轮 P1-1：孤儿任务（审查行缺失）若带调用账本行，
                    # 删除任务行会 ON DELETE CASCADE 连坐账本证据（含已结算
                    # 与 unknown）——绝不删除，只置 failed 并如实报告「该库
                    # 上传暂受严格检查拦截，需离线迁移保留证据」；无账本行
                    # 的死行才安全删除（消除 FK 违例，上传恢复）。
                    # 归属不匹配/状态矛盾/损坏 → 置 failed（审查行存在，无
                    # FK 违例，不影响上传），审查内容一律不触碰。
                    conn.execute("ROLLBACK TO SAVEPOINT reap_one")
                    if isinstance(exc, _OrphanJob):
                        # codex 三轮 P2-4：核对全部级联子表（子表本身可能不
                        # 存在——旧库无该功能；无表=无证据）。任一子表有行
                        # → 保留任务行待离线迁移；全空才安全删除
                        has_child_rows = False
                        for child in _ORPHAN_CHILD_TABLES:
                            _tbl = conn.execute(
                                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                                (child,)).fetchone()
                            if _tbl and conn.execute(
                                f"SELECT 1 FROM {_ORPHAN_CHILD_QUOTED[child]} WHERE job_id=? LIMIT 1",
                                (job["id"],)).fetchone() is not None:
                                has_child_rows = True
                                break
                        if has_child_rows:
                            conn.execute("UPDATE owned_review_jobs SET status='failed' "
                                         "WHERE id=? AND status='running'", (job["id"],))
                            logger.error("账户任务恢复：孤儿任务 %s 带调用账本证据，"
                                         "保留任务行待离线迁移；该库上传暂受严格检查拦截",
                                         job["id"])
                        else:
                            conn.execute("DELETE FROM owned_review_jobs WHERE id=? AND status='running'",
                                         (job["id"],))
                            logger.error("账户任务恢复：删除无账本证据的孤儿任务行 %s（审查行缺失）",
                                         job["id"])
                    else:
                        conn.execute("UPDATE owned_review_jobs SET status='failed' WHERE id=? AND status='running'",
                                     (job["id"],))
                        logger.error("账户任务恢复：隔离坏记录 %s（%s，审查行保留待离线迁移）",
                                     job["id"], type(exc).__name__)
                    conn.execute("RELEASE SAVEPOINT reap_one")
                    quarantined += 1
                except Exception:
                    # 暂时性故障（codex P2：清理钩子/数据库抖动）：回滚本条后
                    # 原样上抛——整批本轮中止、下轮重试，绝不误标为坏记录
                    conn.execute("ROLLBACK TO SAVEPOINT reap_one")
                    conn.execute("RELEASE SAVEPOINT reap_one")
                    raise
            if quarantined:
                logger.error("账户任务恢复：本批隔离 %d 条坏记录，需离线迁移核查", quarantined)
            return len(jobs) - quarantined
