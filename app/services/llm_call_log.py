"""LLM 调用账本（宪法 P0-D1：LLMCallRecord）。

规范依据（llm-position-authority-phased-development.md 22 节）：
每次 LLM 调用必须产生可持久化、可脱敏的调用记录，能回答——调用了几次、
读了哪里、失败在哪里、哪些输出被门禁拒绝、花了多久和多少钱。

设计：
- 记录点在 transport（llm_client.chat_completion）单点：provider/model/
  latency/outcome/usage 天然可得，六个调用节点零散接线只补 node 标签与
  review_id；
- node 与 review_id 通过 contextvar 由调用点注入（record_node 上下文管理器）；
- 存储为 store 同库独立表 llm_calls，TTL 随 reviews 过期一并清理
  （store._purge_expired），不记 API Key / 完整 prompt / 完整合同正文；
- 账本自身故障绝不影响审查主链（emit 全程吞异常+warning）。
"""
from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import sqlite3
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from collections.abc import Generator
from typing import Any, Optional

logger = logging.getLogger(__name__)

# 调用点注入的节点上下文
_current_node: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "llm_call_node", default=None
)
_current_review: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "llm_call_review", default=None
)


@contextlib.contextmanager
def record_node(node: str, review_id: Optional[str] = None) -> Generator[None, None, None]:
    """上下文管理器：包裹一段调用，标记 node 与 review_id。

    用法（调用点）：
        with record_node("objection", review_id=rid):
            out = run_objections(...)
    嵌套以内层为准（contextvar 语义天然支持）。
    """
    t1 = _current_node.set(node)
    t2 = _current_review.set(review_id)
    try:
        yield
    finally:
        _current_node.reset(t1)
        _current_review.reset(t2)


def current_node() -> Optional[str]:
    return _current_node.get()


def current_review_id() -> Optional[str]:
    return _current_review.get()


@dataclass
class LLMCallRecord:
    """单次 LLM 调用记录（字段对齐规范 22 节 LLMCallRecord）。"""

    call_id: str
    node: str                      # precheck | model_map | model_reduce | quality_map | quality_reduce | objection | ask
    provider: str                  # deepseek | zhipu | xai
    model: str
    prompt_version: Optional[str] = None       # P0-D4 的 PROMPT_VERSION（调用点注入，transport 侧可为空）
    document_version: Optional[str] = None     # 输入范围：文档版本（调用点注入）
    chars_sent: Optional[int] = None           # system+user 字符数（prompt 装饰含入，精确 input_scope 由公共 Coverage 承担）
    truncated: Optional[bool] = None
    review_id: Optional[str] = None
    attempt: int = 1
    started_at: Optional[str] = None           # ISO 时间
    latency_ms: Optional[int] = None
    outcome: str = "success"       # success | timeout | provider_error | parse_failed | gate_rejected | budget_exceeded
    usage: dict[str, Any] = field(default_factory=dict)   # input_tokens/output_tokens/cost，provider 未返回填 null
    gate_counts: dict[str, Any] = field(default_factory=dict)  # {accepted, rejected}
    error_detail: Optional[str] = None         # 脱敏后的错误摘要（不含供应商内部细节）

    def to_row(self) -> tuple:
        d = asdict(self)
        return (
            d["call_id"], d["review_id"], d["node"], d["provider"], d["model"],
            d["prompt_version"], d["document_version"], d["chars_sent"],
            1 if d["truncated"] else 0 if d["truncated"] is False else None,
            d["attempt"], d["started_at"], d["latency_ms"], d["outcome"],
            json.dumps(d["usage"], ensure_ascii=False),
            json.dumps(d["gate_counts"], ensure_ascii=False),
            d["error_detail"],
        )


def _db_path() -> Path:
    # 与 store 同库同 TTL 域（审查数据清，账本跟着清）；
    # store 未导出模块级常量，此处按同一 env 约定取路径
    from app.services import store as store_module

    env = (store_module.os.getenv("STORE_DB_PATH") or "").strip()
    return Path(env if env else store_module._DEFAULT_DB)


def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS llm_calls ("
        "call_id TEXT PRIMARY KEY,"
        "review_id TEXT,"
        "node TEXT NOT NULL,"
        "provider TEXT NOT NULL,"
        "model TEXT NOT NULL,"
        "prompt_version TEXT,"
        "document_version TEXT,"
        "chars_sent INTEGER,"
        "truncated INTEGER,"
        "attempt INTEGER DEFAULT 1,"
        "started_at TEXT,"
        "latency_ms INTEGER,"
        "outcome TEXT NOT NULL,"
        "usage TEXT,"
        "gate_counts TEXT,"
        "error_detail TEXT,"
        "created_at REAL NOT NULL)"
    )


def emit(record: LLMCallRecord) -> None:
    """落一条调用记录。账本故障绝不影响审查主链。"""
    try:
        conn = sqlite3.connect(_db_path())
        try:
            _ensure_table(conn)
            conn.execute(
                "INSERT OR REPLACE INTO llm_calls VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                record.to_row() + (time.time(),),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        logger.warning("LLM call log emit failed (non-fatal)", exc_info=True)


def new_call_id() -> str:
    return uuid.uuid4().hex[:16]


def purge_expired(max_age_seconds: float) -> int:
    """清理超龄调用记录（store TTL 到期时同步调用）。返回删除条数。"""
    try:
        conn = sqlite3.connect(_db_path())
        try:
            _ensure_table(conn)
            cur = conn.execute(
                "DELETE FROM llm_calls WHERE created_at < ?",
                (time.time() - max_age_seconds,),
            )
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        logger.warning("LLM call log purge failed (non-fatal)", exc_info=True)
        return 0


def query(review_id: Optional[str] = None, limit: int = 200) -> list[dict[str, Any]]:
    """查询调用记录（管理员排障用；默认按时间倒序）。"""
    conn = sqlite3.connect(_db_path())
    try:
        _ensure_table(conn)
        if review_id:
            cur = conn.execute(
                "SELECT * FROM llm_calls WHERE review_id=? ORDER BY created_at DESC LIMIT ?",
                (review_id, limit),
            )
        else:
            cur = conn.execute(
                "SELECT * FROM llm_calls ORDER BY created_at DESC LIMIT ?", (limit,)
            )
        cols = [d[0] for d in cur.description]
        out: list[dict[str, Any]] = []
        for row in cur.fetchall():
            d = dict(zip(cols, row))
            for jcol in ("usage", "gate_counts"):
                if isinstance(d.get(jcol), str):
                    try:
                        d[jcol] = json.loads(d[jcol])
                    except json.JSONDecodeError:
                        d[jcol] = {}
            out.append(d)
        return out
    finally:
        conn.close()
