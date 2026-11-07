"""补写调用的持久额度与单条互斥；数据随审查单过期清除。"""
from __future__ import annotations

import sqlite3
import errno
import hashlib
import os
import uuid
from contextlib import closing
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from app.services import store as store_module

MAX_ATTEMPTS_PER_REVIEW = 4
MAX_CALLS_PER_REVIEW = 36


class CallGuard:
    """持锁凭证；关闭后不能用来认领或恢复任务。"""

    def __init__(self, review_id: str, item_id: str) -> None:
        self.review_id = review_id
        self.item_id = item_id
        self.active = True


@contextmanager
def item_lock(review_id: str, item_id: str) -> Iterator[CallGuard | None]:
    """依托数据库同目录的固定数量条带锁，进程退出后操作系统自动释放。

    SQLite WAL 本就要求同机文件系统；锁不支持跨主机网络文件系统。
    条带发生碰撞时安全地返回忙，不会并发发送模型请求。
    """
    db_path = Path(store_module.store._path).resolve()
    directory = db_path.parent / (db_path.name + ".remediation-locks")
    directory.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256((review_id + "\x00" + item_id).encode("utf-8")).digest()
    path = directory / f"{digest[0]:02x}.lock"
    with open(path, "a+b") as stream:
        if stream.seek(0, os.SEEK_END) == 0:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                raise
            yield None
            return
        guard = CallGuard(review_id, item_id)
        try:
            yield guard
        finally:
            guard.active = False
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _check_guard(guard: CallGuard, review_id: str, item_id: str) -> None:
    if not guard.active or (guard.review_id, guard.item_id) != (review_id, item_id):
        raise RuntimeError("补写锁凭证无效")


def _create(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS remediation_calls ("
        "review_id TEXT NOT NULL, item_id TEXT NOT NULL,"
        "attempts INTEGER NOT NULL, reserved_calls INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL,"
        "lock_version INTEGER NOT NULL DEFAULT 0, active_reserved INTEGER NOT NULL DEFAULT 0,"
        "started_calls INTEGER NOT NULL DEFAULT 0, generation_token TEXT NOT NULL DEFAULT '',"
        "PRIMARY KEY (review_id, item_id))"
    )
    columns = {row[1] for row in conn.execute("PRAGMA table_info(remediation_calls)")}
    if "reserved_calls" not in columns:
        conn.execute("ALTER TABLE remediation_calls ADD COLUMN reserved_calls INTEGER NOT NULL DEFAULT 0")
        conn.execute("UPDATE remediation_calls SET reserved_calls=attempts")
    for column in ("lock_version", "active_reserved", "started_calls"):
        if column not in columns:
            conn.execute(f"ALTER TABLE remediation_calls ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0")
    if "generation_token" not in columns:
        conn.execute("ALTER TABLE remediation_calls ADD COLUMN generation_token TEXT NOT NULL DEFAULT ''")
    # 旧版本的 UPSERT 不认识 generation_token；若它再次开始任务，自动撤销
    # 锁协议标记，避免新版把仍在运行的旧进程误判为已退出。
    conn.execute(
        "CREATE TRIGGER IF NOT EXISTS remediation_legacy_attempt_guard "
        "AFTER UPDATE OF attempts, state ON remediation_calls "
        "WHEN NEW.state='processing' AND NEW.attempts>OLD.attempts "
        "AND NEW.generation_token=OLD.generation_token BEGIN "
        "UPDATE remediation_calls SET lock_version=0 "
        "WHERE review_id=NEW.review_id AND item_id=NEW.item_id; END"
    )


def reserve(review_id: str, item_id: str, calls: int = 1, *, guard: CallGuard | None = None) -> str:
    """跨线程/进程原子预留；孤儿任务仅在持有同一条带锁时识别。"""
    if guard is not None:
        _check_guard(guard, review_id, item_id)
    if calls < 1 or calls > MAX_CALLS_PER_REVIEW:
        raise ValueError("补写模型调用预留量超出范围")
    with closing(store_module.store._conn()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        _create(conn)
        review = conn.execute("SELECT created_at FROM reviews WHERE id=?", (review_id,)).fetchone()
        if review is None or store_module.store._is_expired(review[0]):
            conn.commit()
            return "expired"
        row = conn.execute(
            "SELECT state, lock_version, active_reserved, started_calls "
            "FROM remediation_calls WHERE review_id=? AND item_id=?",
            (review_id, item_id),
        ).fetchone()
        if row and row[0] == "processing":
            if guard is not None and row[1] == 1:
                # 上次拥有锁的进程已经退出；只释放尚未发出的额度。
                unstarted = max(0, row[2] - row[3])
                conn.execute(
                    "UPDATE remediation_calls SET state='interrupted', "
                    "reserved_calls=reserved_calls-?, active_reserved=0, started_calls=0 "
                    "WHERE review_id=? AND item_id=?", (unstarted, review_id, item_id))
                conn.commit()
                return "interrupted"
            if guard is not None and row[1] == 0:
                conn.commit()
                return "legacy_processing"
            conn.commit()
            return "processing"
        if row and row[0] == "success":
            conn.commit()
            return "success"
        used_attempts, used_calls = conn.execute(
            "SELECT COALESCE(SUM(attempts), 0), COALESCE(SUM(reserved_calls), 0) "
            "FROM remediation_calls WHERE review_id=?",
            (review_id,),
        ).fetchone()
        if used_attempts >= MAX_ATTEMPTS_PER_REVIEW or used_calls + calls > MAX_CALLS_PER_REVIEW:
            conn.commit()
            return "exhausted"
        conn.execute(
            "INSERT INTO remediation_calls (review_id, item_id, attempts, reserved_calls, state, "
            "lock_version, active_reserved, started_calls, generation_token) "
            "VALUES (?, ?, 1, ?, 'processing', ?, ?, 0, ?) ON CONFLICT(review_id, item_id) "
            "DO UPDATE SET attempts=attempts+1, reserved_calls=reserved_calls+excluded.reserved_calls, "
            "state='processing', lock_version=excluded.lock_version, "
            "active_reserved=excluded.active_reserved, started_calls=0, "
            "generation_token=excluded.generation_token",
            (review_id, item_id, calls, 1 if guard else 0, calls, uuid.uuid4().hex),
        )
        conn.commit()
        return "reserved"


def note_call_started(guard: CallGuard) -> None:
    """发起请求前持久记数；崩溃时把已发请求按可能计费保守计算。"""
    _check_guard(guard, guard.review_id, guard.item_id)
    with closing(store_module.store._conn()) as conn, conn:
        cur = conn.execute(
            "UPDATE remediation_calls SET started_calls=started_calls+1 "
            "WHERE review_id=? AND item_id=? AND state='processing' AND lock_version=1 "
            "AND started_calls<active_reserved", (guard.review_id, guard.item_id))
        if cur.rowcount != 1:
            raise RuntimeError("补写调用没有有效预留")


def finish(review_id: str, item_id: str, *, success: bool) -> None:
    with closing(store_module.store._conn()) as conn, conn:
        _create(conn)
        conn.execute(
            "UPDATE remediation_calls SET state=? WHERE review_id=? AND item_id=? "
            "AND state='processing'",
            ("success" if success else "failed", review_id, item_id),
        )


def _purge(conn: sqlite3.Connection, review_ids: list[str]) -> None:
    marks = ",".join("?" for _ in review_ids)
    conn.execute(f"DELETE FROM remediation_calls WHERE review_id IN ({marks})", review_ids)


store_module.PURGE_HOOKS.append(_purge)
