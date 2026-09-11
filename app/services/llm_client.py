"""供应商 HTTP transport（外部审计二轮 PR-E 抽取）。

三家供应商（DeepSeek/智谱/xAI）同为 OpenAI 兼容 /chat/completions，
HTTP 细节只有一处实现；llm_ask 只负责「问什么、模型怎么选、答案怎么
验证」。测试打点：patch app.services.llm_client.httpx.Client。
"""
from __future__ import annotations

import httpx


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
    msg = data["choices"][0]["message"]
    content = (msg.get("content") or "").strip()
    if not content and fallback_reasoning:
        content = (msg.get("reasoning_content") or "").strip()
    return content
