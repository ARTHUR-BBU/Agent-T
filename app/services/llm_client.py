"""供应商 HTTP transport（外部审计二轮 PR-E 抽取）。

三家供应商（DeepSeek/智谱/xAI）同为 OpenAI 兼容 /chat/completions，
HTTP 细节只有一处实现；llm_ask 只负责「问什么、模型怎么选、答案怎么
验证」。测试打点：patch app.services.llm_client.httpx.Client。
"""
from __future__ import annotations

import time

import httpx

from app.services import llm_call_log


def chat_completion(
    *,
    api_key: str,
    url: str,
    model: str,
    system: str,
    user: str,
    temperature: float,
    timeout: float,
    fallback_reasoning: bool = False,
) -> str:
    """OpenAI 兼容 chat/completions 单轮调用，返回 assistant 文本。

    fallback_reasoning：部分 GLM 推理模型会把文本短暂放在
    reasoning_content——content 为空时回退读取（智谱专用，行为保持）。
    """
    # 宪法 P0-D1：transport 是全部 LLM 调用的单点，调用账本在此记录
    # （provider/model/latency/outcome/usage 天然可得；node/review_id 由
    # 调用点经 record_node 注入）。账本故障绝不影响调用本身。
    provider = "deepseek"
    if "bigmodel.cn" in url or "zhipu" in url:
        provider = "zhipu"
    elif "x.ai" in url:
        provider = "xai"
    _t0 = time.monotonic()
    _rec = llm_call_log.LLMCallRecord(
        call_id=llm_call_log.new_call_id(),
        node=llm_call_log.current_node() or "unknown",
        provider=provider,
        model=model,
        review_id=llm_call_log.current_review_id(),
        chars_sent=len(system) + len(user),
    )
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": temperature,
    }
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(url, headers=headers, json=payload)
            if resp.status_code >= 400:
                detail = resp.text[:300]
                raise httpx.HTTPStatusError(
                    f"{resp.status_code} {detail}",
                    request=resp.request,
                    response=resp,
                )
            data = resp.json()
    except httpx.TimeoutException as exc:
        _rec.latency_ms = int((time.monotonic() - _t0) * 1000)
        _rec.outcome = "timeout"
        llm_call_log.emit(_rec)
        raise exc
    except httpx.HTTPStatusError as exc:
        _rec.latency_ms = int((time.monotonic() - _t0) * 1000)
        _rec.outcome = "provider_error"
        _rec.error_detail = str(exc)[:200]
        llm_call_log.emit(_rec)
        raise exc
    _rec.latency_ms = int((time.monotonic() - _t0) * 1000)
    usage = data.get("usage") or {}
    if usage:
        # provider 未返回的字段填 null，不伪估精确值（规范 22 节）
        _rec.usage = {
            "input_tokens": usage.get("prompt_tokens"),
            "output_tokens": usage.get("completion_tokens"),
            "cost": None,
        }
    msg = data["choices"][0]["message"]
    content = (msg.get("content") or "").strip()
    if not content and fallback_reasoning:
        content = (msg.get("reasoning_content") or "").strip()
    _rec.outcome = "success" if content else "parse_failed"
    llm_call_log.emit(_rec)
    return content
