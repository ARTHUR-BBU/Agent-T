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
import threading
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
_current_prompt_version: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "llm_prompt_version", default=None
)
# B2-3 两项升级专项：trace_id 上下文（一次用户操作全链同号；单点生成于
# upload/ask 入口——施工卡 spec-b23-trail-budget §2.1，模块禁止自生成）
_current_trace: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "llm_call_trace", default=None
)


@contextlib.contextmanager
def record_node(
    node: str,
    review_id: Optional[str] = None,
    prompt_version: Optional[str] = None,
    trace_id: Optional[str] = None,
) -> Generator[None, None, None]:
    """上下文管理器：包裹一段调用，标记 node/review_id/prompt_version/trace_id。

    用法（调用点）：
        with record_node("objection", review_id=rid,
                         prompt_version=objection_prompts.PROMPT_VERSION):
            out = run_objections(...)
    嵌套以内层为准（contextvar 语义天然支持）。trace_id 只透传（入口单点
    生成），此处不提供生成逻辑。
    """
    tokens = [
        _current_node.set(node),
        _current_review.set(review_id),
        _current_prompt_version.set(prompt_version),
    ]
    trace_tok = _current_trace.set(trace_id) if trace_id is not None else None
    try:
        yield
    finally:
        if trace_tok is not None:
            _current_trace.reset(trace_tok)
        for var, tok in zip(
            (_current_node, _current_review, _current_prompt_version), tokens
        ):
            var.reset(tok)


@contextlib.contextmanager
def record_trace(trace_id: str) -> Generator[None, None, None]:
    """仅设置 trace 上下文（后台线程入口用：审查 daemon 线程 contextvar
    不自动继承，worker 顶部显式设置后，管线内 record_node 天然读到）。"""
    tok = _current_trace.set(trace_id)
    try:
        yield
    finally:
        _current_trace.reset(tok)


def set_trace(trace_id: str) -> "contextvars.Token[Optional[str]]":
    """非上下文管理器形态（大函数不便整体缩进时用，配 reset_trace）。"""
    return _current_trace.set(trace_id)


def reset_trace(token: "contextvars.Token[Optional[str]]") -> None:
    _current_trace.reset(token)


def current_node() -> Optional[str]:
    return _current_node.get()


def current_review_id() -> Optional[str]:
    return _current_review.get()


def current_prompt_version() -> Optional[str]:
    return _current_prompt_version.get()


def current_trace_id() -> Optional[str]:
    return _current_trace.get()


def short_hash(text: str) -> str:
    """提示词/原文指纹：SHA256 前 16 位（哈希不含内容原文，无泄露面）。"""
    import hashlib
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]


@dataclass
class LLMCallRecord:
    """单次 LLM 调用记录（字段对齐规范 22 节 LLMCallRecord）。

    B2-3 两项升级专项新列（spec-b23-trail-budget §1.1/§2）：
    - trace_id：一次用户操作全链关联键（upload/ask 入口单点生成）
    - raw_output：解析后白名单结构化回执（settle 落，2048 截断）
    - prompt_hash：实际发送提示词全文 SHA256 前 16 位
    """

    call_id: str
    node: str                      # precheck | model_map | model_reduce | quality_map | quality_reduce | objection | ask | category_suggest
    provider: str                  # deepseek | zhipu | xai | cloudflare | typesafe
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
    trace_id: Optional[str] = None             # 全链关联键（transport 自动回读上下文）
    raw_output: Optional[str] = None           # 白名单结构化回执（settle 阶段落账）
    prompt_hash: Optional[str] = None          # 提示词全文指纹（transport 计算）

    def to_row(self) -> dict[str, Any]:
        """按列名导出（B2-3 P1-1：禁位置写入——列序漂移会写错列）。"""
        d = asdict(self)
        d["truncated"] = (
            1 if d["truncated"] else 0 if d["truncated"] is False else None
        )
        for jcol in ("usage", "gate_counts"):
            d[jcol] = json.dumps(d[jcol], ensure_ascii=False)
        return d


def _db_path() -> Path:
    # 与 store 同库同 TTL 域（审查数据清，账本跟着清）；
    # store 未导出模块级常量，此处按同一 env 约定取路径
    from app.services import store as store_module

    env = (store_module.os.getenv("STORE_DB_PATH") or "").strip()
    return Path(env if env else store_module._DEFAULT_DB)


# B2-3 P1-1（终审版）：规范列序——所有写入/查询一律按列名，位置写入清零
_COLUMNS = (
    "call_id", "review_id", "node", "provider", "model",
    "prompt_version", "document_version", "chars_sent", "truncated",
    "attempt", "started_at", "latency_ms", "outcome", "usage",
    "gate_counts", "error_detail", "trace_id", "raw_output",
    "prompt_hash", "created_at",
)

# 迁移锁（并发首启守卫：两线程同判缺列不撞车；ALTER 再包 BEGIN IMMEDIATE）
_migrate_lock = threading.Lock()


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
        "trace_id TEXT,"
        "raw_output TEXT,"
        "prompt_hash TEXT,"
        "created_at REAL NOT NULL)"
    )
    _migrate(conn)


def _migrate(conn: sqlite3.Connection) -> None:
    """旧库幂等加列（B2-3 真实迁移验收：升级→读历史→写新 三步测试锁定）。

    只增不删、向前兼容（施工卡 §1.1.3）：常规回滚不得删列或恢复旧库——
    回退必须到显式列名兼容版本或前向热修。
    """
    with _migrate_lock:
        existing = {row[1] for row in conn.execute("PRAGMA table_info(llm_calls)")}
        for col in ("trace_id", "raw_output", "prompt_hash"):
            if col in existing:
                continue
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(f"ALTER TABLE llm_calls ADD COLUMN {col} TEXT")
                conn.execute("COMMIT")
            except sqlite3.OperationalError as exc:
                conn.execute("ROLLBACK") if conn.in_transaction else None
                if "duplicate column name" in str(exc).lower():
                    continue  # 并发迁移幂等良性
                raise


def emit(record: LLMCallRecord) -> None:
    """落一条调用记录。账本故障绝不影响审查主链。"""
    try:
        conn = sqlite3.connect(_db_path())
        try:
            _ensure_table(conn)
            row = record.to_row()
            row["created_at"] = time.time()
            # 显式列名写入（P1-1）：旧库加列后列序漂移不再影响写入
            cols = ", ".join(_COLUMNS)
            marks = ", ".join("?" for _ in _COLUMNS)
            conn.execute(
                f"INSERT OR REPLACE INTO llm_calls ({cols}) VALUES ({marks})",
                tuple(row[c] for c in _COLUMNS),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        logger.warning("LLM call log emit failed (non-fatal)", exc_info=True)


def settle(
    call_id: str,
    outcome: str,
    raw_output: Optional[str] = None,
    error_detail: Optional[str] = None,
) -> bool:
    """解析后账本结算（B2-3 P1-3：先创建、后结算——「请求成功」≠「解析成功」）。

    仅允许 success → {success, parse_failed} 迁移（provider_error/timeout 为
    transport 终态，不可改写）；raw_output 超长在此截断（2048+标记，防模型
    复述合同原文换字段重存——内容白名单由调用方保证，此处兜底长度）。
    返回是否发生了结算（False=无匹配行或已被终态定格）。
    """
    try:
        if raw_output is not None and len(raw_output) > 2048:
            raw_output = raw_output[:2048] + "...[truncated]"
        conn = sqlite3.connect(_db_path())
        try:
            _ensure_table(conn)
            cur = conn.execute(
                "UPDATE llm_calls SET outcome=?, raw_output=COALESCE(?, raw_output),"
                " error_detail=COALESCE(?, error_detail)"
                " WHERE call_id=? AND outcome='success'",
                (outcome, raw_output, error_detail, call_id),
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        logger.warning("LLM call log settle failed (non-fatal)", exc_info=True)
        return False


def emit_skip(
    node: str,
    provider: str,
    model: str,
    *,
    trace_id: Optional[str] = None,
    review_id: Optional[str] = None,
    prompt_version: Optional[str] = None,
    outcome: str = "budget_exceeded",
    error_detail: Optional[str] = None,
) -> None:
    """「未发出的调用」显式落账（B2-3 P1-4：预算拦截零网络请求、不进
    transport——没有本接口，拦截只是内存私账，对账查无此事）。"""
    emit(LLMCallRecord(
        call_id=new_call_id(), node=node, provider=provider, model=model,
        prompt_version=prompt_version, review_id=review_id, trace_id=trace_id,
        outcome=outcome, error_detail=error_detail,
    ))


def backfill_review_id(review_id: str, trace_id: str) -> int:
    """审查单创建后按 trace 回填关联（P0-D1 注记补全：单创建于调用之后）。

    幂等（review_id IS NULL 才填）；失败只 warning 不伤主链。
    """
    try:
        conn = sqlite3.connect(_db_path())
        try:
            _ensure_table(conn)
            cur = conn.execute(
                "UPDATE llm_calls SET review_id=?"
                " WHERE trace_id=? AND review_id IS NULL",
                (review_id, trace_id),
            )
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        logger.warning("LLM call log backfill failed (non-fatal)", exc_info=True)
        return 0


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


def query(
    review_id: Optional[str] = None,
    limit: int = 200,
    trace_id: Optional[str] = None,
) -> list[dict[str, Any]]:
    """查询调用记录（管理员排障用；默认按时间倒序；可按 review/trace 过滤）。"""
    conn = sqlite3.connect(_db_path())
    try:
        _ensure_table(conn)
        if trace_id:
            cur = conn.execute(
                "SELECT * FROM llm_calls WHERE trace_id=? ORDER BY created_at DESC LIMIT ?",
                (trace_id, limit),
            )
        elif review_id:
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
