"""供应商 HTTP transport（外部审计二轮 PR-E 抽取）。

三家供应商（DeepSeek/智谱/xAI）同为 OpenAI 兼容 /chat/completions，
HTTP 细节只有一处实现；llm_ask 只负责「问什么、模型怎么选、答案怎么
验证」。测试打点：patch app.services.llm_client.httpx.Client。
"""
from __future__ import annotations

import json
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
        prompt_version=llm_call_log.current_prompt_version(),
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
    except httpx.RequestError as exc:
        # Codex P2：ConnectError/DNS/TLS 等网络层失败同样落账
        # （此前只捕 Timeout/HTTPStatusError，网络失败成为账目盲区）
        _rec.latency_ms = int((time.monotonic() - _t0) * 1000)
        _rec.outcome = "provider_error"
        _rec.error_detail = f"request_error: {type(exc).__name__}"
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


def typesafe_judgment(
    *,
    api_key: str,
    url: str,
    model: str,
    state: str,
    instructions: str,
    criteria: dict,
    timeout: float,
    truncated: bool = False,
) -> str:
    """TypeSafe System One 判断调用（非 OpenAI 兼容形态，B2-3 批3）。

    与 chat_completion 同遵守 transport 单点纪律：LLMCallRecord 在此落盘
    （node 由调用点经 record_node 注入）。返回 answers 的原始 JSON 文本，
    解析与白名单校验由调用方负责。
    """
    _t0 = time.monotonic()
    _rec = llm_call_log.LLMCallRecord(
        call_id=llm_call_log.new_call_id(),
        node=llm_call_log.current_node() or "unknown",
        provider="typesafe",
        model=model,
        prompt_version=llm_call_log.current_prompt_version(),
        review_id=llm_call_log.current_review_id(),
        chars_sent=len(state) + len(instructions),
        truncated=truncated,
    )
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {
        "state": state,
        "model": model,
        "questions": {
            "category": {
                "type": "choice",
                "instructions": instructions,
                "criteria": criteria,
            }
        },
    }
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(url, headers=headers, json=payload)
            if resp.status_code >= 400:
                raise httpx.HTTPStatusError(
                    f"{resp.status_code} {resp.text[:300]}",
                    request=resp.request, response=resp)
            try:
                data = resp.json()
            except ValueError as exc:
                _rec.latency_ms = int((time.monotonic() - _t0) * 1000)
                _rec.outcome = "provider_error"
                _rec.error_detail = "invalid_json_response"
                llm_call_log.emit(_rec)
                raise httpx.RequestError("invalid_json_response", request=resp.request) from exc
    except httpx.TimeoutException as exc:
        _rec.latency_ms = int((time.monotonic() - _t0) * 1000)
        _rec.outcome = "timeout"
        llm_call_log.emit(_rec)
        raise exc
    except (httpx.HTTPStatusError, httpx.RequestError) as exc:
        _rec.latency_ms = int((time.monotonic() - _t0) * 1000)
        _rec.outcome = "provider_error"
        _rec.error_detail = str(exc)[:200]
        llm_call_log.emit(_rec)
        raise exc
    _rec.latency_ms = int((time.monotonic() - _t0) * 1000)
    usage = data.get("usage", {})
    _rec.usage = {"input_tokens": usage.get("input_tokens"),
                  "output_tokens": usage.get("output_tokens")}
    answers = data.get("answers", {})
    _rec.gate_counts = {"answers": len(answers)}
    llm_call_log.emit(_rec)
    return json.dumps(answers.get("category", {}), ensure_ascii=False)
