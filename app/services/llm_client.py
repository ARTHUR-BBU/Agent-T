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


# 按节点的输出上限（max_tokens，真水龙头——M6.5 收官回测费用闸要求）。
# 依据=生产账本实测输出分布（2026-10-22：precheck max 1326 / model_review 6150 /
# objection 4654 / quality 12156）× 约 2~2.5 倍余量；未列节点走 DEFAULT。
# 超限=生成截断，下游按既有软降级路径处理（LLM 层输出不进规则结论）。
NODE_MAX_OUTPUT_TOKENS: dict[str, int] = {
    "precheck": 3000,
    "model_review": 13000,
    "quality": 25000,
    "objection": 10000,
    "ask": 8000,
}
DEFAULT_MAX_OUTPUT_TOKENS = 16000


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
    输出上限按节点强制（NODE_MAX_OUTPUT_TOKENS，费用闸真水龙头）。
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
        # B2-3 两项升级专项：trace 全链关联+提示词指纹（哈希不含原文）
        trace_id=llm_call_log.current_trace_id(),
        prompt_hash=llm_call_log.short_hash(f"{system}\x00{user}"),
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
        # 按节点强制输出上限（费用闸：M6.5 收官回测 P1——出水量必须有阀门）
        "max_tokens": NODE_MAX_OUTPUT_TOKENS.get(
            llm_call_log.current_node() or "", DEFAULT_MAX_OUTPUT_TOKENS),
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
            # 畸形 200 响应同归 provider_error（GLM 审计+codex 裁定 2026-10-23）：
            # HTTP 200 但 JSON 坏/缺 choices/结构错 = 请求可能已计费而账本无行，
            # 费用哨兵与证据链的盲区——必须在抛出前落账。
            try:
                data = resp.json()
            except ValueError as exc:
                raise ValueError(f"invalid_json_response: {exc}") from exc
            choices = data.get("choices") if isinstance(data, dict) else None
            if (not choices or not isinstance(choices[0], dict)
                    or not isinstance(choices[0].get("message"), dict)):
                raise ValueError(
                    f"malformed_success_response: choices={str(choices)[:120]}")
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
    except ValueError as exc:
        # 畸形 200 响应（坏 JSON/缺 choices/结构错）：落账后按原异常受控上抛
        # （ValueError 与 httpx 异常族互斥，不存在双重落账）
        _rec.latency_ms = int((time.monotonic() - _t0) * 1000)
        _rec.outcome = "provider_error"
        _rec.error_detail = str(exc)[:200]
        llm_call_log.emit(_rec)
        raise
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


def decision_judgment(
    *,
    api_key: str,
    url: str,
    model: str,
    state: str,
    instructions: str,
    criteria: dict,
    timeout: float,
    truncated: bool = False,
    attempt: int = 1,
) -> tuple[str, str]:
    """决策模型判断调用（TypeSafe Jev / Cloudflare Clef 同构形态，B2-3 批3）。

    请求 {state, model, questions} → 响应 answers（choice/置信度），两家同构；
    Clef 响应多一层 result 包裹，在此剥平。与 chat_completion 同遵守 transport
    单点纪律：LLMCallRecord 在此落盘（node 由调用点经 record_node 注入）。

    B2-3 两项升级专项（spec-b23-trail-budget §1.2 先创建后结算）：成功时返回
    `(call_id, 原始 answers JSON 文本)`——transport 只判 transport 层事实
    （HTTP/协议层坏=provider_error，超时=timeout）；业务解析成败由调用方拿
    call_id 走 llm_call_log.settle 终态结算（success/parse_failed）。
    """
    _t0 = time.monotonic()
    provider = "cloudflare" if "cloudflare" in url or "/ai/run/" in url else "typesafe"
    _rec = llm_call_log.LLMCallRecord(
        call_id=llm_call_log.new_call_id(),
        node=llm_call_log.current_node() or "unknown",
        provider=provider,
        model=model,
        prompt_version=llm_call_log.current_prompt_version(),
        review_id=llm_call_log.current_review_id(),
        chars_sent=len(state) + len(instructions),
        truncated=truncated,
        attempt=attempt,
        trace_id=llm_call_log.current_trace_id(),
        prompt_hash=llm_call_log.short_hash(
            json.dumps({"instructions": instructions, "criteria": criteria,
                        "state": state}, ensure_ascii=False)
        ),
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
    usage = data.get("usage") or data.get("result", {}).get("usage") or {}
    _rec.usage = {"input_tokens": usage.get("input_tokens"),
                  "output_tokens": usage.get("output_tokens")}
    # Clef（Workers AI）多一层 result 包裹；TypeSafe 直出 answers
    answers = data.get("answers") or data.get("result", {}).get("answers") or {}
    _rec.gate_counts = {"answers": len(answers)}
    # 先创建（临时态 success=「transport 层成功」）：业务终态由调用方 settle
    llm_call_log.emit(_rec)
    return _rec.call_id, json.dumps(answers.get("category", {}), ensure_ascii=False)
