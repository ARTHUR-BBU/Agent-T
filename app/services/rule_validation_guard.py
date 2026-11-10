"""Transactional check for unresolved validation-process incidents."""
from __future__ import annotations

import sqlite3
import time


def validation_state(conn: sqlite3.Connection) -> str:
    # 表不存在 = 验证基础设施从未初始化（rule_validation_service 建表时
    # 同时装了防删/防在线复位触发器，已初始化的库不可能丢失控制行）——
    # 「无历史验证事件」按 ready 处理；有表之后的一切事件态照旧 fail-closed。
    # （全量回归 2026-11-11 暴露：versioned 审核在未初始化库上此前直接
    # OperationalError 崩溃——既不是放行也不是拦截，是坏的失败方式。）
    names = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name IN ('rule_validation_control','rule_validation_jobs')")}
    if not names:
        return "ready"
    if "rule_validation_control" not in names or "rule_validation_jobs" not in names:
        # 只建了一半（历史上不可能出现，防御性 fail-closed）
        return "blocked"
    row = conn.execute("SELECT blocked FROM rule_validation_control WHERE id=1").fetchone()
    if row is None or row["blocked"] != 0:
        return "blocked"
    running = conn.execute("SELECT lease_until FROM rule_validation_jobs WHERE status='running' LIMIT 1").fetchone()
    if running is None:
        return "ready"
    return "blocked" if running["lease_until"] <= time.time() else "running"


def validation_blocked(conn: sqlite3.Connection) -> bool:
    return validation_state(conn) != "ready"


def require_validation_ready(conn: sqlite3.Connection) -> None:
    if validation_blocked(conn):
        raise RuntimeError("测试正在进行或进程状态待检查，暂不能审核通过或发布")
