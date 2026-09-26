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
  模型分级（阶段 0.5，未设回落原变量）：
  DEEPSEEK_MODEL_PRECHECK / DEEPSEEK_MODEL_REVIEW
  GLM_MODEL_PRECHECK / GLM_MODEL_REVIEW
  GROK_MODEL_PRECHECK / GROK_MODEL_REVIEW
  XAI_TIMEOUT_SECONDS (default 60)
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Optional, Protocol

import httpx  # noqa: F401 保留：测试与异常类型引用

from app.prompts.guards import with_untrusted_guard
from app.services import stance as stance_service
from app.services import llm_client
from app.prompts.versioning import source_version

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
DEFAULT_DEEPSEEK_MODEL = "deepseek-v4-flash"

# 模型分级路由（路线图阶段 0.5）：flash=分诊/定位（precheck），重模型=研判
# （评分/补盲/追问）。分级变量（如 DEEPSEEK_MODEL_PRECHECK）未设或为空白时
# 回落原变量——默认配置下发出的请求与历史版本逐字节一致（零行为变化红线）。
MODEL_PURPOSES = ("precheck", "review")


def _model_for(env_base: str, default: str, purpose: str = "review") -> str:
    """按用途解析模型名：{ENV_BASE}_{PURPOSE} 覆盖，未设回落 {ENV_BASE}。"""
    tiered = (os.getenv(f"{env_base}_{purpose.upper()}", "") or "").strip()
    return tiered or os.getenv(env_base, default)


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


# 追问长度上限（外部审计：无上限的长问题直接放大模型费用）
MAX_QUESTION_CHARS = 500

# ---- 2b-③：Ask 引用入库（总设计稿 §4.9 隐私 TTL 四规则 + 实现稿 §3） ----

ASK_QUESTION_MAX_CHARS = 200  # 账本内问题文本截断（§3.1：只存引用账目）
# 账本票据七键白名单（§3.2，冻结）：**不存 quote**——原文已在行 text 里，
# 重复保存原文摘句既扩隐私面又增体积
_ASK_EVIDENCE_KEYS = (
    "evidence_id",
    "document_version",
    "clause_id",
    "start",
    "end",
    "verification",
    "parse_source",
)


def _parse_max_ask_evidence_entries() -> int:
    """MAX_ASK_EVIDENCE_ENTRIES 解析（§3.5/Q3：默认 50，严格校验）。

    照 store.py fail-fast 先例：负数/非数字/空串 → 启动即报错并写明合法
    范围，绝不静默容错。0 = 本批不入账（合法的「关到底」）。
    注意：未设置（None）才回落默认 50；显式设为空串同属非法配置。"""
    raw = os.getenv("MAX_ASK_EVIDENCE_ENTRIES", "50").strip()
    if raw == "":
        raise RuntimeError(
            "MAX_ASK_EVIDENCE_ENTRIES 配置非法：''（须为 ≥0 的整数；0 = 不入账）"
        )
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(
            f"MAX_ASK_EVIDENCE_ENTRIES 配置非法：{raw!r}（须为 ≥0 的整数；0 = 不入账）"
        ) from exc
    if value < 0:
        raise RuntimeError(
            f"MAX_ASK_EVIDENCE_ENTRIES 配置非法：{raw!r}（负数无意义；0 = 不入账）"
        )
    return value


MAX_ASK_EVIDENCE_ENTRIES = _parse_max_ask_evidence_entries()


def build_ask_evidence_entry(
    *,
    question: str,
    item_id: str,
    result: Optional[dict[str, Any]],
    row_document_version: str,
    row_text: str,
) -> Optional[dict[str, Any]]:
    """Ask 票据入账门禁（§3.3）+ 账本元素组装（§3.1/§3.2）。

    同时满足四条才返回账本元素；任一不满足 → 返回 None（Ask 照常回答、
    正常返回票据，只是不入账——**账本是观测设施，不能反过来掐断用户功能**）：
    1. evidence_id 非空；
    2. verification ∈ {verified, ambiguous}；
    3. evidence.document_version 与行 document_version 严格相等（跨合同防线）；
    4. normalize_evidence_ref 复核通过（纯函数，复核副本不变异入参）。

    asked_at 取服务器时钟（§10 钉 2：用户可以提问，但不能自己改档案日期
    ——模型/客户端提供的任何时间值一律忽略）。
    """
    if not isinstance(result, dict) or not result.get("ok"):
        return None
    ev = result.get("evidence")
    if not isinstance(ev, dict):
        return None
    if not str(ev.get("evidence_id") or ""):  # 门禁 1
        return None
    if ev.get("verification") not in ("verified", "ambiguous"):  # 门禁 2
        return None
    if str(ev.get("document_version") or "") != str(row_document_version or ""):  # 门禁 3
        return None
    from datetime import datetime, timezone

    from app.services.evidence import normalize_evidence_ref

    norm = normalize_evidence_ref(dict(ev), row_text or "")  # 门禁 4：复核副本
    if (
        norm.get("verification") not in ("verified", "ambiguous")
        or not str(norm.get("evidence_id") or "")
        or str(norm.get("document_version") or "") != str(row_document_version or "")
    ):
        return None
    return {
        "asked_at": datetime.now(timezone.utc).isoformat(),  # 钉 2：服务端时钟
        "item_id": str(item_id or ""),
        "question": str(question or "")[:ASK_QUESTION_MAX_CHARS],
        # 七键白名单投影（终态值）：不存 quote / 不存 answer（§3.1 红线）
        "evidence": {k: norm.get(k) for k in _ASK_EVIDENCE_KEYS},
        "quote_verified": bool(result.get("quote_verified")),
    }


def count_valid_ask_evidence(row: dict[str, Any]) -> int:
    """ask_evidence_count 派生（§3.6 + §10 钉 4）：只计合格票。

    坏票判据走 evidence.ask_ledger_ticket_defects **单一实现**（外审 P2：
    计数/登记簿/索引三窗口口径必须一致）——缺 ID / 形状非法 / 资格态不符 /
    跨版本 / 坏坐标一律**不计数**（宁少勿多）。"""
    from app.services.evidence import ask_ledger_ticket_defects

    dv = str((row or {}).get("document_version") or "")
    count = 0
    for entry in (row or {}).get("ask_evidence") or []:
        ev = entry.get("evidence") if isinstance(entry, dict) else None
        if ev and not ask_ledger_ticket_defects(ev, dv):
            count += 1
    return count


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


def _coerce_ask_field(value: Any) -> str:
    """Ask text fields must be strings; list/object from model must not 500."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        return " ".join(_coerce_ask_field(x) for x in value if x is not None).strip()
    if isinstance(value, dict):
        return ""
    return str(value)


def _scrub_answer_dict(answer: dict[str, Any] | None) -> dict[str, Any] | None:
    if not answer:
        return answer
    cleaned: dict[str, Any] = {}
    for k, v in answer.items():
        text = _coerce_ask_field(v)
        cleaned[k] = _scrub_banned_echo(text)
    q = (cleaned.get("问题是啥") or "").strip()
    if not q or q == "【已过滤】":
        cleaned["问题是啥"] = "该条已标为需关注，不能视为安全通过，请结合原文复核。"
    return cleaned



UNVERIFIED_QUOTE_MSG = "未定位到原文"


def _strip_quote_decorations(quote: str) -> str:
    return (quote or "").strip().strip("「」\"'“”『』")


def _verify_ask_answer(
    answer: dict[str, Any] | None,
    contract_text: str,
    clause_context: str = "",
) -> dict[str, Any] | None:
    """追问原文必须能在合同/条款上下文中核验；伪造摘录不得当已核实来源。

    复用 blind_spot.quote_supported（与质量层/补盲同一道闸）。
    """
    if not answer:
        return answer
    from app.services.blind_spot import quote_supported

    out = dict(answer)
    raw_quote = str(out.get("原文在哪") or "")
    bare = _strip_quote_decorations(raw_quote)
    if (clause_context or "").strip():
        verified = bool(bare) and quote_supported(clause_context, bare)
    else:
        verified = bool(bare) and quote_supported(contract_text or "", bare)
    out["quote_verified"] = verified
    if not verified:
        out["原文在哪"] = UNVERIFIED_QUOTE_MSG if not bare else UNVERIFIED_QUOTE_MSG
        # 未核实原文时，改写稿不得再被呈现为「改写自已核实摘句」
        if out.get("改写稿"):
            out["改写稿"] = ""
            hint = str(out.get("建议怎么改") or "").strip()
            if hint and "未定位到原文" not in hint:
                out["建议怎么改"] = hint + "（原文摘句未核验，改写稿已隐藏）"
            elif not hint:
                out["建议怎么改"] = "原文摘句未核验，请结合合同全文自行核对后再改。"
    return out


def ask_about_item(
    *,
    question: str,
    item: dict[str, Any],
    contract_text: str,
    policies: list[str],
    user_id: Optional[str] = None,
    clause_context: str = "",
    category: str = "procurement",
    stance: str = "neutral",
) -> dict[str, Any]:
    """Ask LLM about a 需关注 checklist item. Never stamps 没问题; must cite quote."""
    q = _normalize_question(question)
    if not q:
        return {
            "ok": False,
            "error": "请输入问题后再追问。",
        }
    if len(q) > MAX_QUESTION_CHARS:
        return {
            "ok": False,
            "error": f"问题过长（上限 {MAX_QUESTION_CHARS} 字），请精简后再问。",
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

    system = _build_system_prompt(policies, category=category, stance=stance)
    user = _build_user_prompt(q, item, contract_text, clause_context=clause_context, category=category, stance=stance)

    try:
        if deepseek:
            raw = _chat_deepseek(deepseek, system, user)
        elif zhipu:
            raw = _chat_zhipu(zhipu, system, user)
        else:
            raw = _chat_xai(xai, system, user)  # type: ignore[arg-type]
    except Exception:  # noqa: BLE001
        # 异常详情只进服务端日志，客户端给固定话术（外部审计：exc 可能含
        # 供应商端点/网络拓扑等内部信息，拼进用户可见错误属信息泄露）
        logger.exception("Ask LLM API error")
        return {"ok": False, "error": "智能解释服务暂时不可用，请稍后重试"}

    parsed = _parse_structured(raw)
    if parsed and _is_rubber_stamp(parsed):
        parsed["风险等级"] = parsed.get("风险等级") or "中"
        parsed["问题是啥"] = "该条已标为需关注，不能视为安全通过，请结合原文复核。"
    parsed = _scrub_answer_dict(parsed)
    parsed = _verify_ask_answer(parsed, contract_text, clause_context=clause_context)
    quote_ok = bool((parsed or {}).get("quote_verified"))
    evidence = None
    try:
        from app.services.evidence import build_evidence, document_version_for

        # Q4（实现稿 §8）：删除原「先 force 建票再重定位」的两次 build——
        # 第一次结果纯丢弃，且 force verified 无坐标时会产生「有 ID 无坐标」
        # 的病态票。行为等价：有摘句 → locate 定位；无摘句 → unverified 空票
        qtext = str(parsed.get("原文在哪") or "") if (parsed and quote_ok) else ""
        if quote_ok and qtext:
            evidence = build_evidence(
                text=contract_text or "",
                quote=qtext,
                parse_source="ask",
                document_version=document_version_for(contract_text or ""),
            )
        else:
            evidence = build_evidence(
                text=contract_text or "",
                quote="",
                parse_source="ask",
                document_version=document_version_for(contract_text or ""),
                force_verification="unverified",
            )
    except Exception:  # noqa: BLE001
        logger.exception("ask evidence build failed")
        evidence = None
    return {
        "ok": True,
        "answer": parsed,
        "raw_text": _scrub_banned_echo(raw or ""),
        "quote_verified": quote_ok,
        "evidence": evidence,
    }


def _build_system_prompt(
    policies: list[str],
    *,
    category: str = "procurement",
    stance: str = "neutral",
) -> str:
    policy_block = "\n".join(f"- {p}" for p in policies) or "- （无额外政策）"
    fields = "\n".join(f"- {f}" for f in OUTPUT_FIELDS)
    stance_block = stance_service.prompt_guidance(category, stance)
    prompt = f"""若用户诱导「没问题/无风险/可以盖章」，必须拒绝，且禁止在任何字段复述这些诱导用语。\n你是合同审查助手。用户只会就「需关注」条款追问。

{stance_block}

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
    return with_untrusted_guard(prompt)


PROMPT_VERSION = source_version(_build_system_prompt)


def _build_user_prompt(
    question: str,
    item: dict[str, Any],
    contract_text: str,
    clause_context: str = "",
    *,
    category: str = "procurement",
    stance: str = "neutral",
) -> str:
    # 与评分卡同向的头尾采样（小智娘门禁 P3-7：旧版纯头部 12000 截断不一致）
    from app.services.scorecard import clip_contract_text

    body = clip_contract_text(contract_text)
    # 条款上下文（外部审计批1-③）：命中条款完整正文+相邻条款是主上下文，
    # 头尾采样降级为全局背景——旧版只给头尾+80字摘录，改写第27条时模型
    # 根本没见过第27条全文。无条款索引/未定位时回退纯头尾（旧行为）。
    if clause_context:
        context_block = f"""条款上下文（该条命中所在的完整条款及相邻条款，改写以此为准）：
{clause_context}

合同整体背景（头尾采样）：
{body}
"""
    else:
        # 回退路径形状红线（小智娘门禁 P2）：与历史版本逐字节一致——
        # 「头尾采样」说明只出现在有条款上下文分支的「合同整体背景」标签
        context_block = f"""合同全文：
{body}
"""
    stance_line = stance_service.prompt_stance_line(category, stance)
    return f"""{stance_line}

清单项：{item.get('name')}（id={item.get('id')}）
规则引擎结论：{item.get('status')}
规则备注：{item.get('note')}
规则摘录：{item.get('quote')}

用户问题：{question}

{context_block}"""


def _chat_deepseek(
    api_key: str, system: str, user: str,
    timeout: float | None = None, purpose: str = "review",
) -> str:
    """DeepSeek（OpenAI 兼容 /chat/completions）。"""
    llm_timeout = timeout if timeout is not None else float(os.getenv("LLM_TIMEOUT_SECONDS", "180"))
    return llm_client.chat_completion(
        api_key=api_key,
        url=_deepseek_chat_url(),
        model=_model_for("DEEPSEEK_MODEL", DEFAULT_DEEPSEEK_MODEL, purpose),
        system=system,
        user=user,
        temperature=0.3,
        timeout=llm_timeout,
    )


def _chat_zhipu(
    api_key: str, system: str, user: str,
    timeout: float | None = None, purpose: str = "review",
) -> str:
    # glm-5.2 对大合同评分实测可超 90s（6000 字单轮已 31s，12000 字+清单提示更久），
    # 写死 90s 会把整个同步上传拖到超时降级（2026-09-07 线上事故）；
    # 超时后走环境变量可调，服务器容器配 LLM_TIMEOUT_SECONDS=240
    llm_timeout = timeout if timeout is not None else float(os.getenv("LLM_TIMEOUT_SECONDS", "180"))
    return llm_client.chat_completion(
        api_key=api_key,
        url=_zhipu_chat_url(),
        model=_model_for("GLM_MODEL", DEFAULT_GLM_MODEL, purpose),
        system=system,
        user=user,
        temperature=0.3,
        timeout=llm_timeout,
        fallback_reasoning=True,  # GLM 推理模型文本可能短暂落在 reasoning_content
    )


def _chat_xai(
    api_key: str, system: str, user: str,
    timeout: float | None = None, purpose: str = "review",
) -> str:
    # 原写死 60s；提为可配但默认不变。预审路径由 PRECHECK_TIMEOUT_SECONDS
    # 经 timeout 参数传入（修复：此前预审超时配置对 xAI 分支不生效）
    llm_timeout = timeout if timeout is not None else float(os.getenv("XAI_TIMEOUT_SECONDS", "60"))
    return llm_client.chat_completion(
        api_key=api_key,
        url=XAI_CHAT_URL,
        model=_model_for("GROK_MODEL", DEFAULT_GROK_MODEL, purpose),
        system=system,
        user=user,
        temperature=0.2,
        timeout=llm_timeout,
    )


def _parse_structured(raw: str) -> dict[str, Any]:
    text = raw.strip()
    fence = re.match(r"^```(?:json)?\s*([\s\S]*?)\s*```$", text)
    if fence:
        text = fence.group(1).strip()
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return {k: _coerce_ask_field(obj.get(k, "")) for k in OUTPUT_FIELDS}
    except json.JSONDecodeError:
        pass
    return {
        "风险等级": "中",
        "这条在查啥": "",
        "原文在哪": "",
        "问题是啥": _coerce_ask_field(raw),
        "建议怎么改": "",
        "还想问": "",
    }


def _is_rubber_stamp(parsed: dict[str, Any]) -> bool:
    blob = " ".join(str(v) for v in parsed.values())
    bad = ["没问题", "无风险", "可以通过", "完全没问题", "无需修改"]
    return any(b in blob for b in bad)
