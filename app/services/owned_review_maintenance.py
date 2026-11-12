"""Bounded recovery for the explicitly enabled local account experiment."""
from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
from contextlib import closing

from app.api.routes_local_accounts import _database_path
from app.services import store as store_module
from app.services.account_store import AccountStore
from app.services.owned_llm_ledger import OwnedLLMLedger
from app.services.owned_review_jobs import OwnedReviewJobs
from app.services.owned_review_store import OwnedReviewStore
from app.services.store import ReviewStore

logger = logging.getLogger(__name__)
_LIMIT = 100


def enabled() -> bool:
    return (store_module._ACCOUNT_MODE == "1" and os.getenv("ACCOUNT_LOCAL_ENABLED") == "1"
            and os.getenv("CONTRACT_ACCOUNT_UPLOAD_TRIAL") == "1"
            and os.getenv("CONTRACT_ACCOUNT_READ_TRIAL") == "1"
            and os.getenv("RULE_EXECUTION_DB_TRIAL") == "1")


def recover_once() -> tuple[int, int]:
    """Reconcile first: an interrupted outbound request has an unknown outcome."""
    path = _database_path()
    if not path.is_file():
        raise RuntimeError("账户数据库不存在")
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as conn:
        names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {"account_users", "account_sessions", "reviews", "owned_review_jobs"}.issubset(names):
        raise RuntimeError("账户数据库尚未初始化")
    accounts = AccountStore(path)
    # 数据行完整性扫描跳过（见 OwnedReviewJobs.__init__ 注记）——损坏库
    # 恰恰是恢复的对象；reap_stale 的隔离机制负责自愈违例行
    jobs = OwnedReviewJobs(OwnedReviewStore(accounts, ReviewStore(str(path))),
                           data_integrity_check=False)
    if "owned_llm_attempts" in names:
        # 账本同样可能带孤儿行（codex P1-1）：先放宽构造，孤儿收口在
        # reconcile 内部完成；常规路径（上传等）仍走严格检查
        ledger = OwnedLLMLedger(jobs, data_integrity_check=False)
    else:
        ledger = None
    attempts = ledger.reconcile(limit=_LIMIT) if ledger is not None else 0
    stale = jobs.reap_stale(limit=_LIMIT)
    # codex 二轮 P1-3 + 三轮 P2-5：两类残留分开报告——只有「任务行或
    # 审查行确实缺失」才构成 SQLite 数据级 FK 违例（上传受阻）；归属
    # 不一致（任务与审查都在但 owner 不同）不构成 FK 违例，上传不受阻，
    # 只需核查。不给出未经验证的结论。
    if ledger is not None:
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as conn:
            _fk_orphans = conn.execute(
                "SELECT COUNT(*) FROM owned_llm_attempts a WHERE NOT EXISTS ("
                "SELECT 1 FROM owned_review_jobs j WHERE j.id=a.job_id) OR NOT EXISTS ("
                "SELECT 1 FROM owned_review_jobs j2 JOIN reviews r ON r.id=j2.review_id "
                "WHERE j2.id=a.job_id)").fetchone()[0]
            _owner_mismatch = conn.execute(
                "SELECT COUNT(*) FROM owned_llm_attempts a "
                "JOIN owned_review_jobs j ON j.id=a.job_id "
                "JOIN reviews r ON r.id=j.review_id "
                "WHERE r.owner_user_id != j.owner_user_id").fetchone()[0]
        if _fk_orphans:
            logger.error("账户任务恢复：账本存在 %d 行孤儿记录（证据保留），"
                         "数据级外键违例未消除——该库上传暂受严格检查拦截，"
                         "需离线迁移修复", _fk_orphans)
        if _owner_mismatch:
            logger.error("账户任务恢复：账本存在 %d 行归属不一致记录（审查行在但"
                         "属于其他账户），不构成外键违例、上传不受阻——建议离线核查",
                         _owner_mismatch)
    return attempts, stale


async def run(stop: asyncio.Event) -> None:
    while not stop.is_set():
        delay = 30
        try:
            attempts, stale = await asyncio.to_thread(recover_once)
            if attempts == _LIMIT or stale == _LIMIT:
                delay = 1
        except Exception as exc:
            logger.error("账户任务恢复失败：%s", type(exc).__name__)
        try:
            await asyncio.wait_for(stop.wait(), timeout=delay)
        except TimeoutError:
            pass
