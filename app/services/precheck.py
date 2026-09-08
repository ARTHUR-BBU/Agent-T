"""LLM 预审：合同分类 + 支持性判断（用户提出、老钱金标、spec 3.2/3.3）。

铁律：分类≠裁判。precheck 只决定「用哪把尺子 / 要不要审」，
输出永不写入 items/档位；降级路径（无 Key/超时/解析失败）退回现状照旧开审。

设计要点（小智娘测试计划对齐）：
- suggested_category 白名单外一律强制置 None（注入逃逸防线）；
- summary 过禁语清洗（LLM 不得借预审之口背书）；
- 解析失败恰好重试 1 次，再失败降级 skip；
- 输入头尾采样（类型特征可能在正文中段，与评分卡同向）。
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
from typing import Any, Callable, Optional

from pydantic import BaseModel, Field

from app.prompts import precheck as precheck_prompts
from app.services import llm_ask

logger = logging.getLogger(__name__)

# 分类不需要全文：头尾采样预算（正文 2400 + 尾部 560，#24 埋中段场景由
# 短合同全文覆盖、长合同头尾夹逼）
MAX_PRECHECK_CHARS = 3000
_PRECHECK_TAIL_CHARS = 560
_CLIP_MARKER = "\n…(中段截断)…\n"

# 预审独立短超时（肉饼门禁 P1：分类 3000 字用不了评分级 180s；
# 长超时在同步路径上等于把上传接口押给 LLM 端点的脾气）
DEFAULT_PRECHECK_TIMEOUT = 30.0

# 预审并发上限（肉饼门禁 P1：precheck 若不设闸，脚本刷上传可绕开
# review 槽位的 429 直接烧 Key 费用。占满时降级跳过预审——可用性优先）
MAX_CONCURRENT_PRECHECKS = 2
_precheck_slots = threading.Semaphore(MAX_CONCURRENT_PRECHECKS)

ChatFn = Callable[[str, str], str]


class PrecheckResult(BaseModel):
    detected_type: str = ""
    is_supported: bool = False
    suggested_category: Optional[str] = None
    confidence: str = "low"  # high / medium / low
    summary: str = ""


class PrecheckOutcome(BaseModel):
    performed: bool = False
    skip_reason: Optional[str] = None  # no_llm_key / llm_error / parse_failed / disabled
    result: Optional[PrecheckResult] = None


def is_precheck_enabled() -> bool:
    return os.getenv("PRECHECK_ENABLED", "true").strip().lower() not in {
        "false",
        "0",
        "no",
    }


def _clip_for_precheck(text: str) -> str:
    if len(text) <= MAX_PRECHECK_CHARS:
        return text
    head = MAX_PRECHECK_CHARS - _PRECHECK_TAIL_CHARS - len(_CLIP_MARKER)
    return text[:head] + _CLIP_MARKER + text[-_PRECHECK_TAIL_CHARS:]


def _default_chat_fn() -> Optional[ChatFn]:
    """供应商链 DeepSeek > 智谱 > xAI，复用 llm_ask 的调用与超时配置。

    超时用预审独立短超时（肉饼门禁 P1），不吃评分级 LLM_TIMEOUT_SECONDS。
    """
    timeout = float(os.getenv("PRECHECK_TIMEOUT_SECONDS", str(DEFAULT_PRECHECK_TIMEOUT)))
    deepseek = llm_ask._deepseek_key()
    zhipu = llm_ask._zhipu_key()
    xai = llm_ask._xai_key()
    if deepseek:
        return lambda s, u: llm_ask._chat_deepseek(deepseek, s, u, timeout=timeout)
    if zhipu:
        return lambda s, u: llm_ask._chat_zhipu(zhipu, s, u, timeout=timeout)
    if xai:
        return lambda s, u: llm_ask._chat_xai(xai, s, u)
    return None


def _parse_payload(raw: str) -> Optional[PrecheckResult]:
    text = (raw or "").strip()
    fence = re.match(r"^```(?:json)?\s*([\s\S]*?)\s*```$", text)
    if fence:
        text = fence.group(1).strip()
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None

    confidence = str(obj.get("confidence") or "low").strip().lower()
    if confidence not in {"high", "medium", "low"}:
        confidence = "low"

    # 白名单校验（注入逃逸防线）：白名单外一律强制 None + 不支持
    supported = supported_category_ids()
    suggested_raw = obj.get("suggested_category")
    suggested = str(suggested_raw).strip() if suggested_raw else None
    if suggested not in supported:
        suggested = None
    is_supported = bool(obj.get("is_supported")) and suggested is not None

    # 禁语清洗 + 限长（肉饼门禁 P3-1）：类型名也要洗——模型跑偏时
    # 「没问题」可从 detected_type 漏出；超长字段直入 store/前端
    detected = llm_ask._scrub_banned_echo(str(obj.get("detected_type") or "").strip())[:100]
    summary = llm_ask._scrub_banned_echo(str(obj.get("summary") or "").strip())[:300]
    return PrecheckResult(
        detected_type=detected,
        is_supported=is_supported,
        suggested_category=suggested,
        confidence=confidence,
        summary=summary,
    )


def supported_category_ids() -> set[str]:
    from app.services.checklist import list_categories

    return {c["id"] for c in list_categories()}


def run_precheck(
    text: str,
    selected_category: str,
    chat_fn: Optional[ChatFn] = None,
) -> PrecheckOutcome:
    """分类预审。任何失败都降级为 skip（照旧开审），绝不阻断主流程。"""
    if not is_precheck_enabled():
        return PrecheckOutcome(skip_reason="disabled")

    chat = chat_fn or _default_chat_fn()
    if chat is None:
        return PrecheckOutcome(skip_reason="no_llm_key")

    # 并发闸门（肉饼门禁 P1）：占满时降级跳过，不排队——排队会把
    # 上传请求拖成变相 DoS，跳过则主流程完全不受影响
    if not _precheck_slots.acquire(blocking=False):
        logger.warning("Precheck slots exhausted, skipping precheck")
        return PrecheckOutcome(skip_reason="busy")

    try:
        from app.services.checklist import list_categories

        system = precheck_prompts.build_system_prompt(list_categories())
        user = precheck_prompts.build_user_prompt(
            _clip_for_precheck(text or ""), selected_category
        )

        for attempt in (1, 2):
            try:
                raw = chat(system, user)
            except Exception:  # noqa: BLE001
                # 异常详情只进日志（对齐 llm_ask 信息泄露防线），降级 skip
                logger.exception("Precheck LLM call failed (attempt %s)", attempt)
                return PrecheckOutcome(skip_reason="llm_error")

            result = _parse_payload(raw)
            if result is not None:
                return PrecheckOutcome(performed=True, result=result)
            logger.warning("Precheck payload parse failed (attempt %s)", attempt)
            system = precheck_prompts.build_retry_system_prompt(
                precheck_prompts.build_system_prompt(list_categories())
            )

        return PrecheckOutcome(skip_reason="parse_failed")
    finally:
        _precheck_slots.release()


def decide_branch(
    outcome: PrecheckOutcome, selected_category: str
) -> dict[str, Any]:
    """把预审结果映射为路由决策（spec 3.3 对账表，含老钱 low 置信度改判）。

    返回：
      action: proceed | confirm_switch | confirm_unsupported
      suspect: low 置信度但有倾向差异 → 照旧开审 + 非阻断「品类存疑」提示
    """
    if not outcome.performed or outcome.result is None:
        return {"action": "proceed", "suspect": False}

    r = outcome.result
    if not r.is_supported:
        return {"action": "confirm_unsupported", "suspect": False}
    if r.suggested_category and r.suggested_category != selected_category:
        if r.confidence == "low":
            # 老钱改判：知情权不能省，打断权必须不给
            return {"action": "proceed", "suspect": True}
        return {"action": "confirm_switch", "suspect": False}
    return {"action": "proceed", "suspect": False}
