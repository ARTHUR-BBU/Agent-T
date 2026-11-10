"""Transactional check for unresolved validation-process incidents."""
from __future__ import annotations

import sqlite3
import time


def validation_state(conn: sqlite3.Connection) -> str:
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
