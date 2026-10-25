# -*- coding: utf-8 -*-
"""M7 三版改写核心服务（B7-1，spec-b7-1-revision-core.md）。

红线落点（老钱终裁 final-ruling-20261024.md + 施工卡）：
- 额度按审查单（review_id）**持久累计**——SQLite 落库，跨请求/重启不重领；
  并发扣减走单条 UPDATE 原子完成（不超发）；
- 只对已打标 item 触发（无 item 架构不可达，且不扣额度）；
- 三版/必填字段不完整 → 整项降级，不展示残缺方案；
- 禁语过滤（第一道）+逐例语义核对（验收本体，B7-1 验收材料）；
- 建议与判定层物理隔离：本模块不写 items，只写 revision_drafts。

账本：每次调用走 llm_client（transport 单点落账，node=revision）；
预算拒绝走 emit_skip 显式落账。交互路径费用闸（非跑批哨兵）见
docs/m7/cost-memo-b7-1.md。
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import tempfile
import uuid
from typing import Any, Callable, Optional

from app.prompts import revision_prompts
from app.services import llm_ask, llm_call_log, llm_client

logger = logging.getLogger(__name__)

# ---- 预算（生产默认 4/3；0/负=不限 仅限开发，生产正数强制门禁见既有挂账） ----
DEFAULT_REVISION_BUDGET = 4
DEFAULT_IMPACT_BUDGET = 3


def revision_limit_from_env() -> int:
    return _int_env("LLM_REVISION_BUDGET_PER_REVIEW", DEFAULT_REVISION_BUDGET)


def impact_limit_from_env() -> int:
    return _int_env("LLM_IMPACT_BUDGET_PER_REVIEW", DEFAULT_IMPACT_BUDGET)


def _int_env(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("%s=%r 不是整数，回退默认 %s", name, raw, default)
        return default


def _db_path() -> str:
    """与审查单库同文件（STORE_DB_PATH / 系统临时目录）——随审查单同生命周期。"""
    env = (os.getenv("STORE_DB_PATH") or "").strip()
    if env:
        return env
    return os.path.join(tempfile.gettempdir(), "agent-t-reviews.db")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path())
    conn.execute(
        "CREATE TABLE IF NOT EXISTS revision_budget ("
        "review_id TEXT PRIMARY KEY,"
        "revision_used INTEGER NOT NULL DEFAULT 0,"
        "impact_used INTEGER NOT NULL DEFAULT 0)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS revision_drafts ("
        "id TEXT PRIMARY KEY,"
        "review_id TEXT NOT NULL,"
        "item_id TEXT NOT NULL,"
        "created_at REAL NOT NULL,"
        "trace_id TEXT,"
        "original_quote TEXT,"
        "payload TEXT NOT NULL,"
        "status TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_drafts_review_item "
        "ON revision_drafts (review_id, item_id, created_at)"
    )
    return conn


def consume_budget(review_id: str, kind: str, limit: int) -> bool:
    """原子扣减一个额度（review_id 维度持久累计，重启/跨请求不重领）。

    True=扣减成功；False=额度已尽（调用方负责 emit_skip 落账+静默降级）。"""
    if limit <= 0:  # 不限（仅开发语义）
        return True
    col = {"revision": "revision_used", "impact": "impact_used"}.get(kind)
    if col is None:
        raise ValueError(f"未知预算类别 {kind!r}")
    with _connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO revision_budget (review_id) VALUES (?)",
            (review_id,),
        )
        cur = conn.execute(
            f"UPDATE revision_budget SET {col} = {col} + 1 "
            f"WHERE review_id = ? AND {col} < ?",
            (review_id, limit),
        )
        return cur.rowcount == 1


def remaining_budget(review_id: str, kind: str, limit: int) -> int:
    col = {"revision": "revision_used", "impact": "impact_used"}.get(kind)
    if col is None:
        raise ValueError(f"未知预算类别 {kind!r}")
    with _connect() as conn:
        row = conn.execute(
            f"SELECT {col} FROM revision_budget WHERE review_id = ?",
            (review_id,),
        ).fetchone()
    used = row[0] if row else 0
    return max(0, limit - used) if limit > 0 else -1


def latest_draft(review_id: str, item_id: str) -> Optional[dict[str, Any]]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT payload, status, trace_id, created_at FROM revision_drafts "
            "WHERE review_id = ? AND item_id = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (review_id, item_id),
        ).fetchone()
    if row is None:
        return None
    return {
        "payload": json.loads(row[0]),
        "status": row[1],
        "trace_id": row[2],
        "created_at": row[3],
    }


# ---- 生成 ----

class _Degraded(Exception):
    """整项降级信号（reason 进账本/返回体，不展示残缺方案）。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _parse_versions(raw: str) -> dict[str, Any]:
    """解析并校验三版输出；任一缺失/为空 → _Degraded("incomplete")（整项降级）。"""
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text)
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise _Degraded("invalid_json") from exc
    versions = data.get("versions") if isinstance(data, dict) else None
    if not isinstance(versions, list) or len(versions) != 3:
        raise _Degraded("incomplete")
    cleaned: list[dict[str, str]] = []
    for v in versions:
        if (not isinstance(v, dict)
                or not str(v.get("stance") or "").strip()
                or not str(v.get("text") or "").strip()):
            raise _Degraded("incomplete")
        cleaned.append({
            "stance": str(v["stance"]).strip(),
            "text": str(v["text"]).strip(),
            "note": str(v.get("note") or "").strip(),
        })
    rationale = str(data.get("rationale") or "").strip()
    if not rationale:
        raise _Degraded("incomplete")
    return {"versions": cleaned, "rationale": rationale}


def _banned_hit(text: str) -> Optional[str]:
    for pat in revision_prompts.BANNED_PATTERNS:
        if pat in text:
            return pat
    return None


def _default_llm_call(api_key: str, system: str, user: str) -> str:
    """生产通道：DeepSeek（OpenAI 兼容），复用 llm_ask 配置助手。"""
    llm_timeout = float(os.getenv("LLM_TIMEOUT_SECONDS", "180"))
    return llm_client.chat_completion(
        api_key=api_key,
        url=llm_ask._deepseek_chat_url(),
        model=llm_ask._model_for("DEEPSEEK_MODEL", llm_ask.DEFAULT_DEEPSEEK_MODEL,
                                 purpose="revision"),
        system=system,
        user=user,
        temperature=0.3,
        timeout=llm_timeout,
    )


def generate_revision(
    store: Any,
    review_id: str,
    item_id: str,
    llm_call: Optional[Callable[[str, str, str], str]] = None,
) -> dict[str, Any]:
    """对已打标 item 生成三版改写（用户逐项点按触发，非审查主路径）。

    返回 {"status": "ok"|"degraded"|"budget_exceeded", ...}；
    review 或 item 不存在 → 返回 {"status": "not_found"}（不扣额度、不调 LLM）。"""
    review = store.get(review_id)
    if review is None:
        return {"status": "not_found", "reason": "review_not_found"}
    item = next((i for i in review.get("items") or [] if i.get("id") == item_id),
                None)
    if item is None:
        # 红线 2：只对已打标 item 触发——未检出项拒绝，不扣额度、不调 LLM
        return {"status": "not_found", "reason": "item_not_found"}

    limit = revision_limit_from_env()
    if not consume_budget(review_id, "revision", limit):
        llm_call_log.emit_skip(
            node="revision",
            provider="deepseek",
            model=llm_ask._model_for("DEEPSEEK_MODEL",
                                     llm_ask.DEFAULT_DEEPSEEK_MODEL,
                                     purpose="revision"),
            review_id=review_id,
            outcome="budget_exceeded",
            error_detail=f"revision budget exhausted (limit={limit})",
        )
        return {"status": "budget_exceeded",
                "message": "本合同的修改建议生成次数已达上限"}

    api_key = llm_ask.get_api_key()
    trace_id = uuid.uuid4().hex[:16]
    llm_call = llm_call or _default_llm_call
    degraded: Optional[_Degraded] = None
    parsed: Optional[dict[str, Any]] = None
    if not api_key:
        degraded = _Degraded("no_llm_key")
    else:
        system = revision_prompts.build_system()
        user = revision_prompts.build_user(item, str(review.get("category_label") or ""))
        try:
            with llm_call_log.record_node(
                "revision", review_id=review_id,
                prompt_version=revision_prompts.PROMPT_VERSION,
                trace_id=trace_id,
            ):
                raw = llm_call(api_key, system, user)
            parsed = _parse_versions(raw)
            hit = _banned_hit(json.dumps(parsed, ensure_ascii=False))
            if hit:
                degraded = _Degraded(f"redline_banned:{hit}")
                parsed = None
        except _Degraded as exc:
            degraded = exc
        except Exception as exc:  # 网络/供应商异常：诚实降级（transport 已落账）
            logger.warning("revision llm error: %s", exc)
            degraded = _Degraded("llm_error")

    if degraded is not None or parsed is None:
        # 整项降级：不展示残缺方案；降级留痕（drafts 表 status=degraded）
        _save_draft(review_id, item_id, trace_id,
                    str(item.get("quote") or ""), {}, "degraded",
                    degraded.reason if degraded else "unknown")
        return {"status": "degraded",
                "reason": degraded.reason if degraded else "unknown",
                "message": "修改建议生成失败，请稍后重试（已留痕，不影响审查结论）"}

    payload = {
        "declaration": revision_prompts.DECLARATION,
        "versions": parsed["versions"],
        "rationale": parsed["rationale"],
        "original_quote": item.get("quote") or "",
        "item_note": item.get("note") or "",
    }
    _save_draft(review_id, item_id, trace_id,
                payload["original_quote"], payload, "ok", "")
    return {"status": "ok", "draft": payload, "trace_id": trace_id,
            "remaining": remaining_budget(review_id, "revision", limit)}


def _save_draft(review_id: str, item_id: str, trace_id: str,
                original_quote: str, payload: dict[str, Any],
                status: str, reason: str) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO revision_drafts (id, review_id, item_id, created_at,"
            " trace_id, original_quote, payload, status) VALUES (?,?,?,?,?,?,?,?)",
            (uuid.uuid4().hex[:16], review_id, item_id,
             __import__("time").time(), trace_id, original_quote,
             json.dumps({"reason": reason, **payload}, ensure_ascii=False),
             status),
        )
