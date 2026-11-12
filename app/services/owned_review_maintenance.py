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
    # codex 二轮 P1-3 + 三轮 P2-5 + 四轮 P2-3/P2-4：报告三分类，不给未经
    # 验证的结论——
    # ① 真实外键违例（PRAGMA foreign_key_check 全表扫描：任务行缺失/审查行
    #    缺失/**任务引用的账户已删除**等一切实际违例）→ 上传受阻，需离线迁移
    # ② 账本归属不一致（任务与审查都在但 owner 不同，含 owner 为 NULL——
    #    NULL 比较必须用 IS NOT，!= 会漏报）→ 不阻塞上传，需核查
    # ③ 日志只记数量与类型，不含合同内容/凭证
    # codex 四轮 P2-4：外键巡检不依赖账本是否存在——任务引用的账户被删
    # 等违例发生在无账本行的库上同样要如实报告（这正是上版的漏报面）
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as conn:
        _fk_violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        _has_ledger = "owned_llm_attempts" in {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        _owner_mismatch = 0
        if _has_ledger:
            _owner_mismatch = conn.execute(
                "SELECT COUNT(*) FROM owned_llm_attempts a "
                "JOIN owned_review_jobs j ON j.id=a.job_id "
                "JOIN reviews r ON r.id=j.review_id "
                "WHERE r.owner_user_id IS NOT j.owner_user_id").fetchone()[0]
    _violation_tables = sorted({row[0] for row in _fk_violations})
    if _violation_tables:
        logger.error("账户任务恢复：数据级外键违例未消除（涉及表：%s）——"
                     "该库上传暂受严格检查拦截，需离线迁移修复",
                     ",".join(_violation_tables))
    if _owner_mismatch:
        logger.error("账户任务恢复：账本存在 %d 行归属不一致记录（含 owner 为空的"
                     "情况），不构成外键违例、上传不受阻——建议离线核查",
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
