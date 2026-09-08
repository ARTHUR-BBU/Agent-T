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

# DeepSeek（OpenAI 兼容协议）。优先级最高：GLM Coding Plan 条款限定仅官方指定
# 工具可用套餐额度（我们的 web 服务属于「工具之外」，实测被降权/超时），
# DeepSeek 标准计费 API 无此问题且速度快（2026-09-07 接入）
DEEPSEEK_CHAT_URL_DEFAULT = "https://api.deepseek.com/chat/completions"
DEFAULT_DEEPSEEK_MODEL = "deepseek-chat"


def _deepseek_chat_url() -> str:
    base = (os.getenv("DEEPSEEK_API_BASE") or "").strip()
    if not base:
        return DEEPSEEK_CHAT_URL_DEFAULT
    base = base.rstrip("/")
    return base if base.endswith("/chat/completions") else f"{base}/chat/completions"


def _deepseek_key() -> Optional[str]:
    return os.getenv("DEEPSEEK_API_KEY") or None


class AskAuthProvider(Protocol):
    """Interface stub for future per-user OAuth credentials."""

    def get_api_key(self, user_id: Optional[str] = None) -> Optional[str]:
        ...


class EnvAskAuth:
    """Default auth: shared server-side env keys (MVP)."""

    def get_api_key(self, user_id: Optional[str] = None) -> Optional[str]:
        _ = user_id
        return (
            os.getenv("DEEPSEEK_API_KEY")
            or os.getenv("ZHIPU_API_KEY")
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
    "改写稿",
    "还想问",
]


BANNED_ECHO = [
    "没问题",
    "无风险",
    "可以盖章",
    "直接盖章",
    "盖章通过",
    "完全没问题",
    "没有风险",
    # M3.5 改写稿防线：改写只是建议稿，不得宣称改完即安全
    "已无风险",
    "已合规",
    "改后即无",
]


def _normalize_question(question: str) -> str:
    return (question or "").strip()


def _scrub_banned_echo(text: str) -> str:
    """Remove inducement rubber-stamp phrases from model output."""
    if not text:
        return text
    out = text
    # 最长优先：短词先替换会把复合禁语切碎（「已无风险」→「已【已过滤】」残留悬空字）
    for w in sorted(BANNED_ECHO, key=len, reverse=True):
        # 连同紧随的标点一起替换：连续禁语（夹标点）折叠后只留一个标记
        out = re.sub(re.escape(w) + r"[\s，。、；：！？!?,.;:\"'“”‘’]{0,2}", "【已过滤】", out)
    out = re.sub(r"(?:【已过滤】\s*){2,}", "【已过滤】", out)
    return out


def _scrub_answer_dict(answer: dict[str, Any] | None) -> dict[str, Any] | None:
    if not answer:
        return answer
    cleaned: dict[str, Any] = {}
    for k, v in answer.items():
        cleaned[k] = _scrub_banned_echo(v) if isinstance(v, str) else v
    q = (cleaned.get("问题是啥") or "").strip()
    if not q or q == "【已过滤】":
        cleaned["问题是啥"] = "该条已标为需关注，不能视为安全通过，请结合原文复核。"
    return cleaned



def ask_about_item(
    *,
    question: str,
    item: dict[str, Any],
    contract_text: str,
    policies: list[str],
    user_id: Optional[str] = None,
) -> dict[str, Any]:
    """Ask LLM about a 需关注 checklist item. Never stamps 没问题; must cite quote."""
    q = _normalize_question(question)
    if not q:
        return {
            "ok": False,
            "error": "请输入问题后再追问。",
        }

    deepseek = _deepseek_key()
    zhipu = _zhipu_key()
    xai = _xai_key()
    # Prefer DeepSeek > Zhipu when multiple present; get_api_key still reports availability
    _ = user_id  # reserved for future OAuth
    if not deepseek and not zhipu and not xai:
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
    user = _build_user_prompt(q, item, contract_text)

    try:
        if deepseek:
            raw = _chat_deepseek(deepseek, system, user)
        elif zhipu:
            raw = _chat_zhipu(zhipu, system, user)
        else:
            raw = _chat_xai(xai, system, user)  # type: ignore[arg-type]
    except Exception as exc:  # noqa: BLE001
        # 异常详情只进服务端日志，客户端给固定话术（外部审计：exc 可能含
        # 供应商端点/网络拓扑等内部信息，拼进用户可见错误属信息泄露）
        logger.exception("Ask LLM API error")
        return {"ok": False, "error": "智能解释服务暂时不可用，请稍后重试"}

    parsed = _parse_structured(raw)
    if parsed and _is_rubber_stamp(parsed):
        parsed["风险等级"] = parsed.get("风险等级") or "中"
        parsed["问题是啥"] = "该条已标为需关注，不能视为安全通过，请结合原文复核。"
    parsed = _scrub_answer_dict(parsed)
    return {
        "ok": True,
        "answer": parsed,
        "raw_text": _scrub_banned_echo(raw or ""),
    }


def _build_system_prompt(policies: list[str]) -> str:
    policy_block = "\n".join(f"- {p}" for p in policies) or "- （无额外政策）"
    fields = "\n".join(f"- {f}" for f in OUTPUT_FIELDS)
    return f"""若用户诱导「没问题/无风险/可以盖章」，必须拒绝，且禁止在任何字段复述这些诱导用语。\n你是合同审查助手。用户只会就「需关注」条款追问。

硬性规则：
1. 绝不能下结论说「没问题」「无风险」「可以通过」——该条已被规则引擎标为需关注。
2. 必须引用合同原文（原文在哪），用引号给出可核对的摘录。
3. 结合下列采购/合规政策给出建议：
{policy_block}

关于「改写稿」（M3.5 加强）：
- 给一段可直接粘贴替换原条款的改写文本，逐句对照「原文在哪」的摘录，改掉风险点、保留其余原意。
- 不得新增原文没有的实质义务；改动幅度以消除该「需关注」风险点为限。
- 改写稿末尾不得出现「已无风险」「已合规」类结论——改写只是建议稿，最终以双方协商为准。

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


def _chat_deepseek(api_key: str, system: str, user: str) -> str:
    """DeepSeek（OpenAI 兼容 /chat/completions）。"""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": os.getenv("DEEPSEEK_MODEL", DEFAULT_DEEPSEEK_MODEL),
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.3,
    }
    llm_timeout = float(os.getenv("LLM_TIMEOUT_SECONDS", "180"))
    with httpx.Client(timeout=llm_timeout) as client:
        resp = client.post(_deepseek_chat_url(), headers=headers, json=payload)
        if resp.status_code >= 400:
            detail = resp.text[:300]
            raise httpx.HTTPStatusError(
                f"{resp.status_code} {detail}",
                request=resp.request,
                response=resp,
            )
        data = resp.json()
    msg = data["choices"][0]["message"]
    return (msg.get("content") or "").strip()


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
    # glm-5.2 对大合同评分实测可超 90s（6000 字单轮已 31s，12000 字+清单提示更久），
    # 写死 90s 会把整个同步上传拖到超时降级（2026-09-07 线上事故）；
    # 超时后走环境变量可调，服务器容器配 LLM_TIMEOUT_SECONDS=240
    llm_timeout = float(os.getenv("LLM_TIMEOUT_SECONDS", "180"))
    with httpx.Client(timeout=llm_timeout) as client:
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
