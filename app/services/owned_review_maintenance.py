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
    # codex 二轮 P1-3 + 三轮 P2-5 + 四轮 P2-3/P2-4 + 五轮 P2-3/P2-4 + 六轮
    # P2-2/P2-4：按真实调用链分类报告，不给未经结论支撑的断言——
    # ① 事实层：「数据库存在外键违例」（有界扫描，超限如实报截断）
    # ② 影响层按真实调用链分三类（六轮 P2-2）：
    #    上传入口 _services 构造 jobs+ledger+worker → 严格检查覆盖
    #    owned_review_jobs / owned_llm_attempts / owned_review_worker_claims；
    #    后续预审路径检查 owned_review_precheck_claims / _results。
    #    各表独立有界探测（PRAGMA foreign_key_check(表名)），不受全库
    #    截断影响——截断时相关表影响如实标「未知」而非「正常」
    # ③ 归属不一致（含 owner 为 NULL，IS NOT 比较）→ 本身不构成外键
    #    违例，上传状态另见外键告警
    _UPLOAD_CRITICAL_TABLES = ("owned_review_jobs", "owned_llm_attempts",
                               "owned_review_worker_claims")
    _PRECHECK_TABLES = ("owned_review_precheck_claims", "owned_review_precheck_results")
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as conn:
        # 关键表逐表有界探测（不受全库截断影响，结果确定）；表可能不存在
        # （缺账本/预审旧库）——不存在即无行，跳过
        _existing_tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        _critical_hits: list[str] = []
        for _table in _UPLOAD_CRITICAL_TABLES + _PRECHECK_TABLES:
            if _table not in _existing_tables:
                continue
            _cursor = conn.execute(f"PRAGMA foreign_key_check({_table})")
            _found = _cursor.fetchmany(50)
            if _found:
                _critical_hits.append(_table)
        # 其余全库违例（事实层）：有界扫描，超限如实报截断——截断后
        # 未扫到的表影响**未知**，不断言其正常
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
        _upload_hit = bool(set(_critical_hits) & set(_UPLOAD_CRITICAL_TABLES))
        _precheck_hit = bool(set(_critical_hits) & set(_PRECHECK_TABLES))
        _owner_mismatch = 0
        _has_ledger = "owned_llm_attempts" in {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if _has_ledger:
            _owner_mismatch = conn.execute(
                "SELECT COUNT(*) FROM owned_llm_attempts a "
                "JOIN owned_review_jobs j ON j.id=a.job_id "
                "JOIN reviews r ON r.id=j.review_id "
                "WHERE r.owner_user_id IS NOT j.owner_user_id").fetchone()[0]
    if _violation_tables:
        logger.error("账户任务恢复：数据库存在外键违例（涉及表：%s%s）",
                     ",".join(sorted(_violation_tables)),
                     "；违例行数较多，全库扫描已截断——未扫到相关表的表，"
                     "影响状态未知" if _fk_truncated else "")
    if _upload_hit:
        logger.error("账户任务恢复：外键违例涉及上传入口严格检查的表"
                     "（%s）——上传相关检查将拒绝，需离线迁移修复",
                     ",".join(sorted(set(_critical_hits) & set(_UPLOAD_CRITICAL_TABLES))))
    if _precheck_hit:
        logger.error("账户任务恢复：外键违例涉及预审路径的表（%s）——"
                     "后续预审相关检查将拒绝，需离线迁移修复",
                     ",".join(sorted(set(_critical_hits) & set(_PRECHECK_TABLES))))
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
