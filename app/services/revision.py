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
import time
import uuid
from typing import Any, Callable, Optional

from app.prompts import revision_prompts
from app.services import llm_ask, llm_call_log, llm_client, store as store_module

logger = logging.getLogger(__name__)

# ---- 预算（生产默认 4/3；0/负=不限 仅限开发，生产正数强制门禁见既有挂账） ----
DEFAULT_REVISION_BUDGET = 4
DEFAULT_IMPACT_BUDGET = 3


def revision_limit_from_env() -> int:
    return _int_env("LLM_REVISION_BUDGET_PER_REVIEW", DEFAULT_REVISION_BUDGET)


def impact_limit_from_env() -> int:
    return _int_env("LLM_IMPACT_BUDGET_PER_REVIEW", DEFAULT_IMPACT_BUDGET)


def _int_env(name: str, default: int) -> int:
    """严格解析：非法/<=0 一律 fail-closed 抛错（启动校验已拦一道，
    运行时再兜一道——绝不静默回退成无限额度，codex P2-3）。"""
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        val = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name}={raw!r} 不是合法整数（fail-closed）") from exc
    if val <= 0:
        raise ValueError(f"{name}={val} 必须为正整数（0/负=无限额度，禁止）")
    return val


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

    True=扣减成功；False=额度已尽（调用方负责 emit_skip 落账+静默降级）。
    limit<=0 直接抛错（无限额度被配置门禁禁止，运行期防御性 fail-closed）。"""
    if limit <= 0:
        raise ValueError(f"额度必须为正整数，实际 {limit}（0/负=无限额度，禁止）")
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
    if limit <= 0:
        raise ValueError(f"额度必须为正整数，实际 {limit}")
    col = {"revision": "revision_used", "impact": "impact_used"}.get(kind)
    if col is None:
        raise ValueError(f"未知预算类别 {kind!r}")
    with _connect() as conn:
        row = conn.execute(
            f"SELECT {col} FROM revision_budget WHERE review_id = ?",
            (review_id,),
        ).fetchone()
    used = row[0] if row else 0
    return max(0, limit - used)


def _purge_hook(conn: sqlite3.Connection, expired_ids: list[str]) -> None:
    """store 过期删除钩子（codex P1-2 复批：清理必须接在实际删除路径上）——
    同连接同事务，审查单删则草稿/预算删，不靠业务侧懒触发。"""
    marks = ",".join("?" for _ in expired_ids)
    conn.execute(
        f"DELETE FROM revision_drafts WHERE review_id IN ({marks})", expired_ids)
    conn.execute(
        f"DELETE FROM revision_budget WHERE review_id IN ({marks})", expired_ids)


# import 时挂载（store 不反向依赖 revision，无循环导入；
# main → routes_revision → revision 链保证应用启动即注册）
store_module.PURGE_HOOKS.append(_purge_hook)


def purge_orphans() -> None:
    """兜底清扫（钩子之外的第二道）：清掉 review 已不存在的草稿与预算行。
    主清理走 store 钩子（同连接同事务）；本函数只兜历史遗留孤儿。"""
    with _connect() as conn:
        try:
            conn.execute(
                "DELETE FROM revision_drafts WHERE review_id NOT IN "
                "(SELECT id FROM reviews)"
            )
            conn.execute(
                "DELETE FROM revision_budget WHERE review_id NOT IN "
                "(SELECT id FROM reviews)"
            )
        except sqlite3.OperationalError:
            # reviews 表尚未创建（冷启动无任何审查单）：无孤儿可清
            logger.debug("purge_orphans skipped: reviews table absent")


def latest_draft(review_id: str, item_id: str) -> Optional[dict[str, Any]]:
    """读最新草稿；自核审查单是否已过期/删除（codex 复批：读取时核验）——
    JOIN reviews 并要求未过 TTL（TTL=0=永不过期时不加时限）。"""
    ttl = store_module._TTL_SECONDS
    cutoff = time.time() - ttl if ttl > 0 else None
    sql = (
        "SELECT d.payload, d.status, d.trace_id, d.created_at "
        "FROM revision_drafts d JOIN reviews r ON r.id = d.review_id "
        "WHERE d.review_id = ? AND d.item_id = ? "
    )
    params: list[Any] = [review_id, item_id]
    if cutoff is not None:
        sql += "AND r.created_at >= ? "
        params.append(cutoff)
    sql += "ORDER BY d.created_at DESC LIMIT 1"
    with _connect() as conn:
        row = conn.execute(sql, params).fetchone()
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
    """严格 Schema（codex P2-1）：稳妥/平衡/进取各一份且互不重复；
    stance/text/note/rationale 必须为非空字符串（数字被 str() 洗成正文
    的通道封死——类型不对即降级）；任一不符 → _Degraded("incomplete")。"""
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
    seen_stances: set[str] = set()
    for v in versions:
        if not isinstance(v, dict):
            raise _Degraded("incomplete")
        stance, txt, note = v.get("stance"), v.get("text"), v.get("note")
        # 类型严格：int/float 等非字符串一律拒绝（不 str() 兜底）
        if (not isinstance(stance, str) or not isinstance(txt, str)
                or not isinstance(note, str)):
            raise _Degraded("incomplete")
        stance, txt, note = stance.strip(), txt.strip(), note.strip()
        if not stance or not txt or not note or stance in seen_stances:
            raise _Degraded("incomplete")
        seen_stances.add(stance)
        cleaned.append({"stance": stance, "text": txt, "note": note})
    if seen_stances != {"稳妥", "平衡", "进取"}:
        raise _Degraded("incomplete")
    rationale = data.get("rationale")
    if not isinstance(rationale, str) or not rationale.strip():
        raise _Degraded("incomplete")
    return {"versions": cleaned, "rationale": rationale.strip()}


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


def _settle_business(trace_id: str, degraded: Optional[_Degraded],
                     raw_output: Optional[str]) -> None:
    """按 trace 反查本节点调用行并做业务终态结算（P2-4①，非致命）。

    ok → success+回执定格；业务降级（invalid_json/incomplete/禁语）→
    parse_failed+原因；llm_error 时 transport 行已是 provider_error/timeout
    终态，settle 无匹配自动不动作。不额外记账、不改造存量节点。"""
    try:
        rows = llm_call_log.query(trace_id=trace_id, limit=5)
        if not rows:
            return
        call_id = rows[0].get("call_id")
        if not call_id:
            return
        if degraded is None:
            llm_call_log.settle(call_id, "success", raw_output=raw_output)
        elif degraded.reason.startswith(("invalid_json", "incomplete", "redline")):
            llm_call_log.settle(call_id, "parse_failed",
                                error_detail=f"revision:{degraded.reason}")
        # llm_error：transport 终态（provider_error/timeout），不可也不应结算
    except Exception:  # noqa: BLE001
        logger.warning("revision settle failed (non-fatal)", exc_info=True)


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
        purge_orphans()  # 审查单已被过期删除：关联草稿/预算一并清（codex P1-2）
        return {"status": "not_found", "reason": "review_not_found"}
    item = next((i for i in review.get("items") or [] if i.get("id") == item_id),
                None)
    if item is None:
        # 红线 2：只对已打标 item 触发——未检出项拒绝，不扣额度、不调 LLM
        return {"status": "not_found", "reason": "item_not_found"}
    # P1-1（codex 2026-10-25）：可改写校验——「未找到/本类不适用/无原文」
    # 的项没有可改写对象，拒绝且不扣额度、不请求模型
    if item.get("status") != "需关注" or not (item.get("quote") or "").strip():
        return {"status": "not_supported", "reason": "item_not_revisable",
                "message": "该项无可改写的原文依据（仅「需关注」且有原文引句的项支持生成修改建议）"}
    purge_orphans()

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

    api_key = llm_ask._deepseek_key()
    trace_id = uuid.uuid4().hex[:16]
    llm_call = llm_call or _default_llm_call
    degraded: Optional[_Degraded] = None
    parsed: Optional[dict[str, Any]] = None
    raw_output: Optional[str] = None
    if not api_key:
        # P2-2（codex）：当前批只接 DeepSeek——只取 DeepSeek 凭据，
        # 缺失本地降级（不得把智谱 Key 发给 DeepSeek 通道）
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
            raw_output = raw
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
        # P2-4①：业务终态结算（复用 B2-3 settle 机制，按 trace 反查本节点
        # 的调用行——成功→带原始回执定格；业务解析/禁语拒绝→parse_failed；
        # transport 层失败（provider_error/timeout）settle 自动无匹配不动作）
        _settle_business(trace_id, degraded, raw_output)

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
        # P2-4②（codex）：保存可回指原文的证据坐标（同句多次出现时定位目标）
        "evidence": {
            "evidence_start": item.get("evidence_start"),
            "evidence_end": item.get("evidence_end"),
            "primary_clause_id": item.get("primary_clause_id") or "",
            "clause_ids": item.get("clause_ids") or [],
        },
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
             time.time(), trace_id, original_quote,
             json.dumps({"reason": reason, **payload}, ensure_ascii=False),
             status),
        )
