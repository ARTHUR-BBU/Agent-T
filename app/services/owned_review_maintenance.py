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
    # codex 二轮 P1-3 + 三轮 P2-5 + 四轮 P2-3/P2-4 + 五轮 P2-3/P2-4：报告
    # 三分类，不给未经结论支撑的断言——
    # ① 事实层：「数据库存在外键违例」（PRAGMA foreign_key_check 有界扫描）
    # ② 影响层：仅当违例涉及**上传相关表**（owned_review_jobs/owned_llm_
    #    attempts——普通上传入口的严格构造检查对象）才报「上传相关检查
    #    将拒绝」；其他扩展表的违例不据此断言上传受阻
    # ③ 归属不一致（含 owner 为 NULL，IS NOT 比较）→ 「本身不构成外键
    #    违例，上传状态另见外键告警」
    # 扫描有界：fetchmany 分批累计，超上限停止并如实报告截断；
    # 巡检不依赖账本是否存在——无账本行的库同样要如实报告（上版漏报面）
    _UPLOAD_CRITICAL_TABLES = {"owned_review_jobs", "owned_llm_attempts"}
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as conn:
        _fk_violations: list[tuple] = []
        _fk_truncated = False
        _cursor = conn.execute("PRAGMA foreign_key_check")
        while True:
            _batch = _cursor.fetchmany(200)
            if not _batch:
                break
            _fk_violations.extend(_batch)
            if len(_fk_violations) >= 500:
                _fk_truncated = True
                break
        _violation_tables = {row[0] for row in _fk_violations}
        _upload_hit = bool(_violation_tables & _UPLOAD_CRITICAL_TABLES)
        _has_ledger = "owned_llm_attempts" in {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        _owner_mismatch = 0
        if _has_ledger:
            _owner_mismatch = conn.execute(
                "SELECT COUNT(*) FROM owned_llm_attempts a "
                "JOIN owned_review_jobs j ON j.id=a.job_id "
                "JOIN reviews r ON r.id=j.review_id "
                "WHERE r.owner_user_id IS NOT j.owner_user_id").fetchone()[0]
    if _violation_tables:
        logger.error("账户任务恢复：数据库存在外键违例（涉及表：%s%s）",
                     ",".join(_violation_tables),
                     "；违例行数较多，扫描结果已截断" if _fk_truncated else "")
    if _upload_hit:
        logger.error("账户任务恢复：外键违例涉及账户任务/调用账本相关表"
                     "（%s）——上传相关检查将拒绝，需离线迁移修复",
                     ",".join(sorted(_violation_tables & _UPLOAD_CRITICAL_TABLES)))
    if _owner_mismatch:
        logger.error("账户任务恢复：账本存在 %d 行归属不一致记录（含 owner 为空的"
                     "情况）；该归属问题本身不构成外键违例，上传状态另见外键告警",
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
