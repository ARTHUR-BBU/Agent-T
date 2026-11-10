# -*- coding: utf-8 -*-
"""B7-6c「看看怎么补」：未找到条目的补写建议服务。

老钱裁定（docs/legal-review-b76bc.md §二）落地：
- 两步强制联动：先查「写在别处/替代安排」→ 确认缺失才出草稿（单次调用
  返回两段结构化结果，查证命中则 draft.needed=False）
- 草稿=中性示范文本：双向中性（不按立场优化）、决策性数值留空
  （____（待填写：××））、头部声明「示范文本，非法律意见」
- 负清单类目（主体/签署/格式条款公平性）**入口级拦截**，不进模型——
  主体是事实不是条款，AI 补写=编造事实；签署是履约动作；不公平条款
  的补写稿有帮一方拟制无效条款之嫌（民法典 497 条）。命中只给行动
  指引，零模型零费用
- 全程不改档位：本服务只产工件，状态归规则引擎
"""
from __future__ import annotations

from app.services.owned_llm_transport import OwnedTransportError

import json
import logging
import re
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, StrictBool, ValidationError


logger = logging.getLogger("uvicorn.error")

PROMPT_VERSION = "remediate-v2-segmented"
_CHUNK_CHARS = 6000
_CHUNK_OVERLAP = 200
MAX_CONTRACT_CHARS = 200_000

# 老钱 2.4 负清单（工程提醒①：类目级拦截，不靠提示词自觉）
_NEGATIVE_IDS = {"subject", "lessor_title", "signature", "unfair_terms"}
# 仅当前四类清单中经本批讨论的一般商业条目可进入模型；新条目默认不开放。
_GENERAL_IDS = {
    "subject_matter", "payment", "term", "breach", "confidentiality", "ip",
    "jurisdiction", "termination", "governing_law", "service_scope", "schedule",
    "acceptance", "confidentiality_scope", "agreement_term", "confidentiality_duration",
    "lease_term", "delivery_acceptance", "use_restriction", "rent_payment", "deposit",
    "early_termination", "renovation", "maintenance", "subletting",
}
_PROTECTED_CONTEXT = re.compile(
    r"劳动合同|劳动者|劳务关系|消费者|未成年人|抵押|质押|"
    r"担保|审批|批准后生效|登记后生效"
)
_DECISION_VALUE = re.compile(
    r"(?:[0-9０-９一二三四五六七八九十百千万两零〇]+\s*"
    r"(?:个?工作日|天|日|个?月|年|小时|分钟|元|万元|亿元|%|％|成)"
    r"|百分之[0-9０-９一二三四五六七八九十百千万两零〇]+)"
)
_SPECIFIC_FORUM = re.compile(r"(?:向|由)(?![^，。；]{0,20}____)[^，。；]{1,20}(?:人民法院|仲裁委员会)")

_GUIDANCE = {
    "subject": (
        "主体信息（资格、资质、授权）是事实不是条款文本，AI 无法代为补写。"
        "请核实签约主体信息后补齐，或咨询专业人士；补入后建议重新提交审查确认。"
    ),
    "signature": (
        "签署与盖章是履约动作不是文本问题：请安排签署（签字/盖章/授权）后，"
        "重新提交审查确认。"
    ),
    "unfair_terms": (
        "该条涉及格式条款与显失公平的效力边界，不宜由 AI 出示范文本——"
        "此处建议咨询专业意见，避免写入可能无效的条款。"
    ),
}

MAX_BLANKS = 6


class _ElsewhereResult(BaseModel):
    model_config = ConfigDict(strict=True)
    found: StrictBool
    summary: str = ""
    quotes: list[str] = []


class _DraftResult(BaseModel):
    model_config = ConfigDict(strict=True)
    needed: StrictBool
    clause_draft: str = ""
    placement: str = ""
    related: list[str] = []
    blanks: list[str] = []


class _ModelResult(BaseModel):
    model_config = ConfigDict(strict=True)
    elsewhere: _ElsewhereResult
    draft: _DraftResult
    refuse_reason: str = ""


def _contract_chunks(text: str) -> list[str]:
    if not text:
        return [""]
    step = _CHUNK_CHARS - _CHUNK_OVERLAP
    return [text[pos:pos + _CHUNK_CHARS]
            for pos in range(0, len(text), step) if pos == 0 or pos + _CHUNK_OVERLAP < len(text)]


def calls_needed(contract_text: str) -> int | None:
    """长合同每段查证一次、最后再生成参考；超限不调用。"""
    if len(contract_text) > MAX_CONTRACT_CHARS:
        return None
    if len(contract_text) <= _CHUNK_CHARS:
        return 1
    return len(_contract_chunks(contract_text)) + 1


def is_negative_item(item: dict[str, Any]) -> bool:
    """负清单命中判定（老钱 2.4 + 工程提醒①：按 id 归属判断）。"""
    iid = str((item or {}).get("id") or "")
    return iid in _NEGATIVE_IDS or iid not in _GENERAL_IDS


def guidance_for(item: dict[str, Any]) -> str:
    iid = str((item or {}).get("id") or "")
    if iid in _GUIDANCE:
        return _GUIDANCE[iid]
    if iid == "lessor_title":
        return _GUIDANCE["subject"]
    return "该检查项暂不提供 AI 补写示范，请结合合同原文咨询专业人士。"


def needs_specialist(item: dict[str, Any], contract_text: str) -> bool:
    """保护领域及审批登记线索命中时，停止通用草稿并转人工核对。"""
    context = " ".join((str(item.get("name") or ""),
                        str(item.get("note") or ""), contract_text or ""))
    return bool(_PROTECTED_CONTEXT.search(context))


def remediate_item(
    *,
    item: dict[str, Any],
    contract_text: str,
    clause_context: str = "",
    category: str = "procurement",
    before_call: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """未找到条目补写建议（纯读合同 + 单次模型调用；不改任何状态）。

    返回结构（经 Schema 校验后外发）：
    {ok, error, mode: elsewhere|draft|error,
     elsewhere: {found, summary, quotes:[..]},
     draft: {needed, clause_draft, placement, related:[], blanks:[..]},
     quote_verified}
    """
    # 负清单：入口级拦截（老钱 2.4）——零模型调用、零费用
    if is_negative_item(item):
        return {
            "ok": True, "mode": "guidance",
            "guidance": guidance_for(item),
            "elsewhere": {"found": False, "summary": "", "quotes": []},
            "draft": {"needed": False, "clause_draft": "", "placement": "",
                      "related": [], "blanks": []},
            "quote_verified": False,
        }

    if needs_specialist(item, contract_text):
        return {"ok": True, "mode": "guidance", "guidance":
                "合同涉及特殊保护、审批登记或担保事项，请先咨询专业人士并核对具体安排。"}

    if not (contract_text or "").strip():
        return {"ok": False, "mode": "error", "error": "合同正文不可用，无法核查补充参考"}
    if calls_needed(contract_text or "") is None:
        return {"ok": False, "mode": "error",
                "error": "合同超过补写查证的长度上限，请人工核对"}

    from app.services import llm_ask
    deepseek = llm_ask._deepseek_key()
    zhipu = llm_ask._zhipu_key()
    xai = llm_ask._xai_key()
    if not (deepseek or zhipu or xai):
        return {"ok": False, "error": "补写建议暂未开通", "mode": "error",
                "elsewhere": {"found": False, "summary": "", "quotes": []},
                "draft": {"needed": False, "clause_draft": "", "placement": "",
                          "related": [], "blanks": []}}

    segmented = len(contract_text) > _CHUNK_CHARS
    def chat(system: str, user: str) -> str:
        if before_call is not None:
            before_call()
        if deepseek:
            return llm_ask._chat_deepseek(deepseek, system, user)
        if zhipu:
            return llm_ask._chat_zhipu(zhipu, system, user)
        return llm_ask._chat_xai(xai, system, user)  # type: ignore[arg-type]

    try:
        if segmented:
            for chunk in _contract_chunks(contract_text):
                found = _parse_scan(chat(_build_scan_prompt(),
                                         f"检查项：{item.get('name')}\n合同片段：\n{chunk}"))
                if found is None or (not found.found and found.quotes):
                    return {"ok": False, "mode": "error", "error": "分段核查未完成，请重试或人工核对"}
                if found.found:
                    return _post_process({"ok": True,
                        "elsewhere": {"found": True, "summary": found.summary,
                                      "quotes": found.quotes[:3]},
                        "draft": {"needed": False, "clause_draft": "", "placement": "",
                                  "related": [], "blanks": []}}, chunk)
        system = _build_system_prompt(category, segmented=segmented)
        user = _build_user_prompt(item, contract_text, clause_context, segmented=segmented)
        raw = chat(system, user)
    except OwnedTransportError:
        raise
    except Exception:  # noqa: BLE001
        logger.exception("Remediate LLM API error")
        return {"ok": False, "error": "智能补写服务暂时不可用，请稍后重试",
                "mode": "error",
                "elsewhere": {"found": False, "summary": "", "quotes": []},
                "draft": {"needed": False, "clause_draft": "", "placement": "",
                          "related": [], "blanks": []}}

    parsed = _parse_result(raw)
    if not parsed.get("ok"):
        return parsed
    return _post_process(parsed, contract_text)


def _build_scan_prompt() -> str:
    from app.prompts.guards import with_untrusted_guard
    return with_untrusted_guard("""检查合同片段是否已有检查项对应的安排或替代安排。
只根据给出的片段判断；存在时给连续逐字原文摘句，不确定时 found=false。
严格只输出 JSON：{"found": true或false, "summary": "简述", "quotes": ["原文摘句"]}。""")


def _parse_scan(raw: str) -> _ElsewhereResult | None:
    text = str(raw or "")
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        return _ElsewhereResult.model_validate_json(text[start:end + 1])
    except ValidationError:
        return None


def _build_system_prompt(category: str, *, segmented: bool = False) -> str:
    from app.prompts.guards import with_untrusted_guard
    coverage = ("系统已分段检查全文但未发现相关内容，模型可能漏检；以下只有首尾摘要，"
                "请只给一般性的示范参考，不要声称已确认缺失。" if segmented else
                "先检查以下合同全文，判断相关内容是否写在别处或存在替代安排。")
    prompt = f"""你是合同审查助手。用户收到的审查报告里有一条检查项显示「未找到」——
即规则引擎在合同中没有检索到该项内容。你的任务分两步，一次完成：

第一步【查证】：{coverage}
如果找到相关内容，给出原文摘句，不出草稿；未找到不等于确认缺失。

第二步【补写】：未发现相关约定时，可给一份中性示范条款草稿，提醒人工复核。
硬性规则：
1. 示范文本必须双向中性——不得按买方/卖方、出租/承租等任何立场优化条款方向
2. 决策性数值一律留空：金额、比例、期限、管辖地等写成「____（待填写：××）」，
   绝不编造或建议具体数值；blanks 数组列出每个留空位的名称
3. 不说「建议约定较高/较低××」——那是立场性意见
4. 输出 JSON，不要输出任何 JSON 以外的文字

输出 JSON 结构：
{{
  "elsewhere": {{"found": true/false,
                "summary": "写在别处时：一句话说明写在哪里、如何覆盖",
                "quotes": ["原文摘句1", "摘句2"]}},
  "draft": {{"needed": true/false,
            "clause_draft": "未发现相关约定时的示范条款正文（含____留空位）",
            "placement": "常见位置参考：类似条款通常出现在「××」等章节附近（仅供排版参考，放在哪里由您决定，不影响条款效力）",
            "related": ["可与第×条相互呼应（须为合同中真实存在的条款）"],
            "blanks": ["金额", "期限"]}},
  "refuse_reason": "仅当该项不适合出示范文本时填写原因，此时 draft.needed=false"
}}

品类上下文：{category}。related 里的条款引用必须是合同原文中真实存在的条款编号——
找不到就留空数组，绝不编造条款号。"""
    return with_untrusted_guard(prompt)

def _build_user_prompt(item: dict[str, Any], contract_text: str,
                       clause_context: str, *, segmented: bool = False) -> str:
    from app.services.scorecard import clip_contract_text
    body = clip_contract_text(contract_text)
    return f"""检查项：{item.get('name')}（id={item.get('id')}）
规则备注：{item.get('note')}
{f"相关条款上下文：{clause_context}" if clause_context else ""}
{'合同首尾摘要（已分段检查，仍须人工核对）' if segmented else '合同全文'}：
{body}

请按系统指令输出 JSON。"""


def _parse_result(raw: str) -> dict[str, Any]:
    """解析模型输出（沿用 ask 的容错口径：码栏剥离→首个大括号截取）。"""
    text = str(raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return {"ok": False, "error": "补写结果解析失败（结构化输出缺失）",
                "mode": "error",
                "elsewhere": {"found": False, "summary": "", "quotes": []},
                "draft": {"needed": False, "clause_draft": "", "placement": "",
                          "related": [], "blanks": []}}
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return {"ok": False, "error": "补写结果解析失败（JSON 非法）",
                "mode": "error",
                "elsewhere": {"found": False, "summary": "", "quotes": []},
                "draft": {"needed": False, "clause_draft": "", "placement": "",
                          "related": [], "blanks": []}}
    if not isinstance(data, dict):
        return {"ok": False, "error": "补写结果解析失败（顶层非对象）",
                "mode": "error",
                "elsewhere": {"found": False, "summary": "", "quotes": []},
                "draft": {"needed": False, "clause_draft": "", "placement": "",
                          "related": [], "blanks": []}}
    try:
        validated = _ModelResult.model_validate(data)
    except ValidationError:
        return {"ok": False, "error": "补写结果解析失败（字段格式不正确）", "mode": "error"}
    elsewhere = validated.elsewhere
    draft = validated.draft
    if len(draft.clause_draft) > 6000:
        return {"ok": False, "error": "补写结果过长，请重试或人工处理", "mode": "error"}
    return {
        "ok": True,
        "elsewhere": {
            "found": elsewhere.found,
            "summary": elsewhere.summary.strip(),
            "quotes": [q.strip() for q in elsewhere.quotes[:3] if q.strip()],
        },
        "draft": {
            "needed": draft.needed,
            "clause_draft": draft.clause_draft.strip(),
            "placement": draft.placement.strip(),
            "related": [r.strip() for r in draft.related[:4] if r.strip()],
            "blanks": [b.strip() for b in draft.blanks[:MAX_BLANKS] if b.strip()],
            "refuse_reason": validated.refuse_reason.strip(),
        },
    }


def _post_process(result: dict[str, Any],
                  contract_text: str) -> dict[str, Any]:
    """三道后处理（老钱裁定的机器可查部分）：
    ①查证命中 → 强制 draft.needed=False（两步联动不可倒置）
    ②摘句核验：quotes 逐条对合同原文做包含匹配，未命中的丢弃并降级标注
    ③查证未命中且草稿被拒/为空 → 如实 error 不装正常"""
    elsewhere: dict[str, Any] = result.get("elsewhere") or {}
    draft: dict[str, Any] = result.get("draft") or {}
    result["elsewhere"] = elsewhere
    result["draft"] = draft
    if elsewhere["found"]:
        draft["needed"] = False

    # 摘句核验（可信度 P1 F02 同源口径：未核验的摘句不得呈现为已核实）——
    # 有摘句且全部/部分命中→quote_verified=True；声明命中但摘句全没核对上
    # →查证结果作废（不当「写在别处」也不出草稿，如实报无可用建议）
    normalized = re.sub(r"\s+", "", str(contract_text or ""))
    kept: list[str] = []
    for q in elsewhere["quotes"]:
        if re.sub(r"\s+", "", q) and re.sub(r"\s+", "", q) in normalized:
            kept.append(q)
    elsewhere["quotes"] = kept
    if elsewhere["found"] and not kept:
        # 声明写在别处但一条摘句都没核实到：不可信，按无可用建议处理
        elsewhere["found"] = False
        elsewhere["summary"] = ""
        draft["needed"] = False
        result["mode"] = "error"
        result["ok"] = False
        result["error"] = "查证结果未能通过原文核验，请稍后重试"
        result["quote_verified"] = False
        return result
    result["quote_verified"] = bool(kept)

    if elsewhere["found"]:
        result["mode"] = "elsewhere"
        draft["needed"] = False
        from app.services.llm_ask import _CONCLUSIVE_LEGAL_ADVICE
        if _CONCLUSIVE_LEGAL_ADVICE.search(elsewhere.get("summary") or ""):
            elsewhere["summary"] = "以下摘句可能与该检查项有关，请结合上下文核对。"
        return result

    if not draft["needed"] or not draft["clause_draft"]:
        refuse = draft.get("refuse_reason") or ""
        result["mode"] = "error"
        result["ok"] = False
        result["error"] = refuse or "这次没有得出可用的补充建议，请稍后重试"
        return result

    # 不让明显的具体数值随草稿流出；无法验证的关联条款也不展示。
    proposed_text = "\n".join([draft["clause_draft"], draft.get("placement") or "",
                               *draft.get("related", []), *draft.get("blanks", [])])
    from app.services.llm_ask import _CONCLUSIVE_LEGAL_ADVICE
    if (_DECISION_VALUE.search(proposed_text) or _SPECIFIC_FORUM.search(proposed_text)
            or _CONCLUSIVE_LEGAL_ADVICE.search(proposed_text)):
        result.update(ok=False, mode="error", error="草稿含有需由您决定的具体内容，请重试或人工处理")
        return result
    verified_related = []
    for relation in draft["related"]:
        numbers = re.findall(r"第[一二三四五六七八九十百千零〇两0-9]+条", relation)
        if numbers and all(number in contract_text for number in numbers):
            verified_related.append(relation)
    draft["related"] = verified_related

    # 确认缺失 → 草稿；查证段如实置空（未命中就不展示假命中）
    result["mode"] = "draft"
    elsewhere["found"] = False
    return result
