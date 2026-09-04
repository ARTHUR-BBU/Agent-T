"""Grok (xAI) HTTP client for follow-up Q&A on 需关注 items only.

Env: XAI_API_KEY or GROK_API_KEY
Stub reserved for future user OAuth login for Grok.
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Optional, Protocol

import httpx

logger = logging.getLogger(__name__)

XAI_CHAT_URL = "https://api.x.ai/v1/chat/completions"
DEFAULT_MODEL = "grok-2-latest"

# ---------------------------------------------------------------------------
# Future: user OAuth login for Grok
# ---------------------------------------------------------------------------
class GrokAuthProvider(Protocol):
    """Interface stub for future per-user OAuth credentials to xAI/Grok.

    When product adds "Login with X / xAI OAuth", implement this protocol
    to resolve a user-scoped API token instead of the shared env key.
    """

    def get_api_key(self, user_id: Optional[str] = None) -> Optional[str]:
        ...


class EnvGrokAuth:
    """Default auth: shared server-side env key (MVP)."""

    def get_api_key(self, user_id: Optional[str] = None) -> Optional[str]:
        # user_id reserved for future OAuth mapping; unused in MVP
        _ = user_id
        return os.getenv("XAI_API_KEY") or os.getenv("GROK_API_KEY") or None


# Placeholder for future OAuth-backed provider:
# class OAuthGrokAuth:
#     def get_api_key(self, user_id: Optional[str] = None) -> Optional[str]:
#         # Look up refresh/access token for user_id, exchange if needed
#         raise NotImplementedError("User OAuth for Grok not yet implemented")


_auth: GrokAuthProvider = EnvGrokAuth()


def get_api_key(user_id: Optional[str] = None) -> Optional[str]:
    return _auth.get_api_key(user_id)


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
    """Ask Grok about a 需关注 checklist item. Never stamps 没问题; must cite quote."""
    api_key = get_api_key(user_id)
    if not api_key:
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
        raw = _chat(api_key, system, user)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Grok API error")
        return {"ok": False, "error": f"Grok 调用失败: {exc}"}

    parsed = _parse_structured(raw)
    # Safety: never allow "没问题" rubber-stamp on 需关注
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
    # Cap contract length for MVP context
    body = contract_text if len(contract_text) <= 12000 else contract_text[:12000] + "\n…(截断)"
    return f"""清单项：{item.get('name')}（id={item.get('id')}）
规则引擎结论：{item.get('status')}
规则备注：{item.get('note')}
规则摘录：{item.get('quote')}

用户问题：{question}

合同全文：
{body}
"""


def _chat(api_key: str, system: str, user: str) -> str:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": os.getenv("GROK_MODEL", DEFAULT_MODEL),
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
    # strip optional ```json fences
    fence = re.match(r"^```(?:json)?\s*([\s\S]*?)\s*```$", text)
    if fence:
        text = fence.group(1).strip()
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return {k: obj.get(k, "") for k in OUTPUT_FIELDS}
    except json.JSONDecodeError:
        pass
    # Fallback: put whole text into 问题是啥
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
