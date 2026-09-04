"""LLM follow-up Q&A for 需关注 items (Zhipu GLM preferred, xAI Grok optional).

Auth resolution:
  1. ZHIPU_API_KEY or GLM_API_KEY → Zhipu OpenAPI
  2. XAI_API_KEY or GROK_API_KEY → xAI Grok
  3. else → error「追问暂未开通」

Env:
  GLM_MODEL (default glm-5.2)
  ZHIPU_API_BASE (default coding plan:
    https://open.bigmodel.cn/api/coding/paas/v4)
  GROK_MODEL (default grok-2-latest)
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Optional, Protocol

import httpx

logger = logging.getLogger(__name__)

DEFAULT_ZHIPU_API_BASE = "https://open.bigmodel.cn/api/coding/paas/v4"


def _zhipu_chat_url() -> str:
    base = (os.getenv("ZHIPU_API_BASE") or DEFAULT_ZHIPU_API_BASE).rstrip("/")
    return f"{base}/chat/completions"

XAI_CHAT_URL = "https://api.x.ai/v1/chat/completions"
DEFAULT_GLM_MODEL = "glm-5.2"
DEFAULT_GROK_MODEL = "grok-2-latest"


class AskAuthProvider(Protocol):
    """Interface stub for future per-user OAuth credentials."""

    def get_api_key(self, user_id: Optional[str] = None) -> Optional[str]:
        ...


class EnvAskAuth:
    """Default auth: shared server-side env keys (MVP)."""

    def get_api_key(self, user_id: Optional[str] = None) -> Optional[str]:
        _ = user_id
        return (
            os.getenv("ZHIPU_API_KEY")
            or os.getenv("GLM_API_KEY")
            or os.getenv("XAI_API_KEY")
            or os.getenv("GROK_API_KEY")
            or None
        )


# Backward-compatible aliases
GrokAuthProvider = AskAuthProvider
EnvGrokAuth = EnvAskAuth

_auth: AskAuthProvider = EnvAskAuth()


def get_api_key(user_id: Optional[str] = None) -> Optional[str]:
    """True-ish if any supported ask provider key is present."""
    return _auth.get_api_key(user_id)


def _zhipu_key() -> Optional[str]:
    return os.getenv("ZHIPU_API_KEY") or os.getenv("GLM_API_KEY") or None


def _xai_key() -> Optional[str]:
    return os.getenv("XAI_API_KEY") or os.getenv("GROK_API_KEY") or None


OUTPUT_FIELDS = [
    "风险等级",
    "这条在查啥",
    "原文在哪",
    "问题是啥",
    "建议怎么改",
    "还想问",
]


def ask_about_item(
    *,
    question: str,
    item: dict[str, Any],
    contract_text: str,
    policies: list[str],
    user_id: Optional[str] = None,
) -> dict[str, Any]:
    """Ask LLM about a 需关注 checklist item. Never stamps 没问题; must cite quote."""
    zhipu = _zhipu_key()
    xai = _xai_key()
    # Prefer Zhipu when both present; get_api_key still reports availability
    _ = user_id  # reserved for future OAuth
    if not zhipu and not xai:
        return {
            "ok": False,
            "error": "追问暂未开通",
        }

    if item.get("status") != "需关注":
        return {
            "ok": False,
            "error": "仅支持对「需关注」条目追问。",
        }

    system = _build_system_prompt(policies)
    user = _build_user_prompt(question, item, contract_text)

    try:
        if zhipu:
            raw = _chat_zhipu(zhipu, system, user)
        else:
            raw = _chat_xai(xai, system, user)  # type: ignore[arg-type]
    except Exception as exc:  # noqa: BLE001
        logger.exception("Ask LLM API error")
        provider = "智谱" if zhipu else "Grok"
        return {"ok": False, "error": f"{provider} 调用失败: {exc}"}

    parsed = _parse_structured(raw)
    if parsed and _is_rubber_stamp(parsed):
        parsed["风险等级"] = parsed.get("风险等级") or "中"
        parsed["问题是啥"] = (
            parsed.get("问题是啥")
            or "该条已标为需关注，不能简单视为没问题，请结合原文复核。"
        )
    return {
        "ok": True,
        "answer": parsed,
        "raw_text": raw,
    }


def _build_system_prompt(policies: list[str]) -> str:
    policy_block = "\n".join(f"- {p}" for p in policies) or "- （无额外政策）"
    fields = "\n".join(f"- {f}" for f in OUTPUT_FIELDS)
    return f"""你是合同审查助手。用户只会就「需关注」条款追问。

硬性规则：
1. 绝不能下结论说「没问题」「无风险」「可以通过」——该条已被规则引擎标为需关注。
2. 必须引用合同原文（原文在哪），用引号给出可核对的摘录。
3. 结合下列采购/合规政策给出建议：
{policy_block}

请用 JSON 对象回答，字段严格为：
{fields}

风险等级取：高 / 中 / 低。
「还想问」给 1-2 个用户可继续追问的短问题。
只输出 JSON，不要 markdown 围栏。"""


def _build_user_prompt(question: str, item: dict[str, Any], contract_text: str) -> str:
    body = contract_text if len(contract_text) <= 12000 else contract_text[:12000] + "\n…(截断)"
    return f"""清单项：{item.get('name')}（id={item.get('id')}）
规则引擎结论：{item.get('status')}
规则备注：{item.get('note')}
规则摘录：{item.get('quote')}

用户问题：{question}

合同全文：
{body}
"""


def _chat_zhipu(api_key: str, system: str, user: str) -> str:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": os.getenv("GLM_MODEL", DEFAULT_GLM_MODEL),
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.3,
    }
    url = _zhipu_chat_url()
    with httpx.Client(timeout=90.0) as client:
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
    if not content:
        # some GLM coding responses put text in reasoning_content briefly
        content = (msg.get("reasoning_content") or "").strip()
    return content


def _chat_xai(api_key: str, system: str, user: str) -> str:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": os.getenv("GROK_MODEL", DEFAULT_GROK_MODEL),
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.2,
    }
    with httpx.Client(timeout=60.0) as client:
        resp = client.post(XAI_CHAT_URL, headers=headers, json=payload)
        resp.raise_for_status()
        data = resp.json()
    return data["choices"][0]["message"]["content"]


def _parse_structured(raw: str) -> dict[str, Any]:
    text = raw.strip()
    fence = re.match(r"^```(?:json)?\s*([\s\S]*?)\s*```$", text)
    if fence:
        text = fence.group(1).strip()
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return {k: obj.get(k, "") for k in OUTPUT_FIELDS}
    except json.JSONDecodeError:
        pass
    return {
        "风险等级": "中",
        "这条在查啥": "",
        "原文在哪": "",
        "问题是啥": raw,
        "建议怎么改": "",
        "还想问": "",
    }


def _is_rubber_stamp(parsed: dict[str, Any]) -> bool:
    blob = " ".join(str(v) for v in parsed.values())
    bad = ["没问题", "无风险", "可以通过", "完全没问题", "无需修改"]
    return any(b in blob for b in bad)
