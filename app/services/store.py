"""SQLite review store（接口与旧内存版完全一致，外部审计 P1：内存仓库无上限）。

- 行数据整体 JSON 序列化存单列，schema 演进零成本；
- 每次写入带过期时间（默认 24h），create 时顺带清理过期行——
  容器重启不丢（进程内数据换持久化），长期运行不涨内存；
- 连接按操作开关（无长连接），天然线程安全，兼容后台审查线程；
- 路径走环境变量 STORE_DB_PATH（容器内默认 /tmp，本地默认系统临时目录），
  也可挂卷持久化。
"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import threading
import time
import uuid
from contextlib import closing
from typing import Any

_DEFAULT_DB = os.path.join(
    tempfile.gettempdir(), "agent-t-reviews.db"
)
# 审查结果默认保留 24h（可 STORE_TTL_HOURS 覆盖；0 = 永不过期）
_TTL_SECONDS = float(os.getenv("STORE_TTL_HOURS", "24")) * 3600


class ReviewStore:
    def __init__(self, db_path: str | None = None, ttl_seconds: float | None = None) -> None:
        self._path = db_path or os.getenv("STORE_DB_PATH") or _DEFAULT_DB
        self._ttl = ttl_seconds if ttl_seconds is not None else _TTL_SECONDS
        self._lock = threading.Lock()
        with self._conn() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS reviews ("
                " id TEXT PRIMARY KEY, created_at REAL NOT NULL, data TEXT NOT NULL)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_reviews_created_at ON reviews (created_at)"
            )
        # 重启后遗留的 processing 行永远等不到后台线程，标记为失败
        # （小智娘 P3-2：否则用户侧会挂到 TTL 过期）
        self._mark_stale_processing()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _mark_stale_processing(self) -> None:
        with self._lock, closing(self._conn()) as conn, conn:
            rows = conn.execute(
                "SELECT id, data FROM reviews WHERE data LIKE '%\"status\": \"processing\"%'"
            ).fetchall()
            for rid, data in rows:
                payload = json.loads(data)
                if payload.get("status") != "processing":
                    continue
                payload["status"] = "error"
                payload["error"] = "服务重启中断，请重新上传"
                conn.execute(
                    "UPDATE reviews SET data = ? WHERE id = ?",
                    (json.dumps(payload, ensure_ascii=False), rid),
                )

    def _purge_expired(self, conn: sqlite3.Connection) -> None:
        if self._ttl <= 0:
            return
        conn.execute("DELETE FROM reviews WHERE created_at < ?", (time.time() - self._ttl,))

    def create(self, **kwargs: Any) -> str:
        rid = uuid.uuid4().hex[:12]
        row = {"id": rid, **kwargs}
        now = time.time()
        with self._lock, closing(self._conn()) as conn, conn:
            self._purge_expired(conn)
            conn.execute(
                "INSERT INTO reviews (id, created_at, data) VALUES (?, ?, ?)",
                (rid, now, json.dumps(row, ensure_ascii=False)),
            )
        return rid

    def get(self, review_id: str) -> dict[str, Any] | None:
        with closing(self._conn()) as conn:
            row = conn.execute(
                "SELECT data FROM reviews WHERE id = ?", (review_id,)
            ).fetchone()
        return json.loads(row[0]) if row else None

    def update(self, review_id: str, **kwargs: Any) -> dict[str, Any] | None:
        with self._lock, closing(self._conn()) as conn, conn:
            row = conn.execute(
                "SELECT data FROM reviews WHERE id = ?", (review_id,)
            ).fetchone()
            if not row:
                return None
            data = json.loads(row[0])
            data.update(kwargs)
            conn.execute(
                "UPDATE reviews SET data = ? WHERE id = ?",
                (json.dumps(data, ensure_ascii=False), review_id),
            )
            return data


store = ReviewStore()
