"""A6 有界主动核验（阶段 3 第一刀）：疑点 → 按条款取证 → 核对摘句/事实 → 待人确认 → 确认后再核。

软件负责证据边界、预算/次数封顶与版本锚定；模型（若有）只可提议下一步核对或解释，
不得改写规则清单档位（Design B）。本期闭环以确定性路径为主，不烧 LLM。

法务五条分流（人审边界，绝不改规则四档）：
1. 异议期偏短 → must_human
2. 验收标准是否另附 → must_human
3. 价款再核对 → machine_ok 仅当 verification=verified，否则升人审
4. 摘句未定位（有摘句但对不上）→ must_human
5. 金额跨条款 → 完全一致且可追原文 → machine_silent，否则 must_human
总原则：证据链闭合且无价值判断 → 机器；商业取舍/另约可能/证据断裂 → 人。
"""
from __future__ import annotations

import hashlib
import logging
import os
import threading
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

from app.services.reason_codes import Reason
from app.services.blind_spot import MAX_QUOTE_CHARS, quote_supported
from app.services.evidence import build_evidence, document_version_for, locate_quote_span

logger = logging.getLogger(__name__)

QuestionStatus = Literal["pending", "confirmed", "disputed", "rechecked"]
ConfirmChoice = Literal["confirm", "dispute"]
TriageDisposition = Literal["must_human", "machine_ok", "machine_silent"]

# 硬预算（可被 env 覆盖；垃圾值回退默认）
DEFAULT_MAX_ROUNDS = 3
DEFAULT_MAX_QUESTIONS = 8
DEFAULT_MAX_CLAUSE_FETCHES = 24
DEFAULT_MAX_RECHECK_PER_Q = 2

_DISCLAIMER = "主动核查只提疑点，不改变清单规则档"


class ConfirmQuestion(BaseModel):
    id: str
    source: str  # quality_obs | blind | pending | fact | rule_attention
    source_ref: str = ""
    question: str = ""
    title: str = ""
    clause_id: Optional[str] = None
    quote: str = ""
    evidence: Optional[dict[str, Any]] = None
    verification: str = "unverified"  # verified|ambiguous|missing|unverified
    fact_ok: Optional[bool] = None
    status: QuestionStatus = "pending"
    human_choice: Optional[ConfirmChoice] = None
    human_note: str = ""
    revised_quote: str = ""
    recheck_count: int = 0
    last_recheck: Optional[dict[str, Any]] = None
    # 法务五条分流：仅 must_human 进入「需你确认」待办；machine_* 不打扰人
    source_subject_key: str = ""  # 批 2b-②：来源对象稳定键（入 claim 身份，防静默剥字——审计 P1）
    claim_id: str = ""  # 批 2b-②：主张编号（防 pack_verify 静默剥字）
    claim_content_hash: str = ""
    evidence_refs: list[dict[str, Any]] = Field(default_factory=list)
    triage: TriageDisposition = "must_human"
    triage_reason: str = ""
    triage_rule: str = ""  # objection_short|acceptance_annex|payment_recheck|quote_unlocated|amount_cross|default


class VerifyInfo(BaseModel):
    available: bool = False
    reason: Optional[str] = None
    rounds_used: int = 0
    max_rounds: int = DEFAULT_MAX_ROUNDS
    clause_fetches_used: int = 0
    max_clause_fetches: int = DEFAULT_MAX_CLAUSE_FETCHES
    max_questions: int = DEFAULT_MAX_QUESTIONS
    max_recheck_per_question: int = DEFAULT_MAX_RECHECK_PER_Q
    questions: list[ConfirmQuestion] = Field(default_factory=list)
    disclaimer: str = _DISCLAIMER
    document_version: str = ""
    # 分流审计：含 machine_ok / machine_silent（不进 questions 待办）
    triage_log: list[dict[str, Any]] = Field(default_factory=list)

    def budget_snapshot(self) -> dict[str, Any]:
        pending = sum(1 for q in self.questions if q.status == "pending")
        return {
            "rounds_remaining": max(0, self.max_rounds - self.rounds_used),
            "clause_fetches_remaining": max(
                0, self.max_clause_fetches - self.clause_fetches_used
            ),
            "questions_total": len(self.questions),
            "questions_pending": pending,
            "max_questions": self.max_questions,
            "max_recheck_per_question": self.max_recheck_per_question,
        }


def _env_int(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        n = int(raw)
    except ValueError:
        logger.warning("%s=%r 不是整数，回退默认 %s", name, raw, default)
        return default
    return n if n >= 1 else default


def limits_from_env() -> dict[str, int]:
    return {
        "max_rounds": _env_int("VERIFY_MAX_ROUNDS", DEFAULT_MAX_ROUNDS),
        "max_questions": _env_int("VERIFY_MAX_QUESTIONS", DEFAULT_MAX_QUESTIONS),
        "max_clause_fetches": _env_int(
            "VERIFY_MAX_CLAUSE_FETCHES", DEFAULT_MAX_CLAUSE_FETCHES
        ),
        "max_recheck_per_question": _env_int(
            "VERIFY_MAX_RECHECK_PER_Q", DEFAULT_MAX_RECHECK_PER_Q
        ),
    }


def empty_verify(*, reason: str = "not_attempted", document_version: str = "") -> dict[str, Any]:
    lim = limits_from_env()
    return VerifyInfo(
        available=False,
        reason=reason,
        max_rounds=lim["max_rounds"],
        max_clause_fetches=lim["max_clause_fetches"],
        max_questions=lim["max_questions"],
        max_recheck_per_question=lim["max_recheck_per_question"],
        document_version=document_version,
    ).model_dump()


def clause_by_id(
    text: str, clause_index: Optional[dict[str, Any]], clause_id: Optional[str]
) -> Optional[dict[str, Any]]:
    """按条款 id 取证：返回元数据 + 原文切片（软件边界，不信任模型编号以外的坐标）。"""
    if not clause_id or not text:
        return None
    for c in (clause_index or {}).get("clauses") or []:
        if str(c.get("id") or "") != str(clause_id):
            continue
        start, end = c.get("start"), c.get("end")
        if not isinstance(start, int) or not isinstance(end, int):
            return None
        if not (0 <= start < end <= len(text)):
            return None
        return {
            "id": str(clause_id),
            "heading": str(c.get("heading") or ""),
            "start": start,
            "end": end,
            "text": text[start:end],
        }
    return None


def verify_quote_against_text(
    text: str,
    quote: str,
    *,
    document_version: str = "",
    clause_id: Optional[str] = None,
    clause_index: Optional[dict[str, Any]] = None,
    parse_source: str = "quality",
) -> dict[str, Any]:
    """核对摘句是否落在合同原文；挂统一证据引用。"""
    evidence = build_evidence(
        text=text or "",
        quote=(quote or "")[:MAX_QUOTE_CHARS],
        parse_source=parse_source,  # type: ignore[arg-type]
        document_version=document_version or document_version_for(text or ""),
        clause_id=clause_id,
        clause_index=clause_index,
    )
    return evidence


# ---------- 法务五条分流 ----------

_PAYMENT_IDS = {"payment", "price", "价款", "价款与支付", "价款支付"}
_PAYMENT_NAME_KEYS = ("价款", "支付", "付款", "预付", "尾款")
_OBJECTION_KEYS = ("异议期", "异议")
_ACCEPTANCE_KEYS = ("验收标准", "另附")
_AMOUNT_NORM_RE = __import__("re").compile(
    r"(?:人民币|￥|¥|RMB)?\s*"
    r"([0-9]+(?:\.[0-9]+)?|[一二三四五六七八九十百千万亿两壹贰叁肆伍陆柒捌玖拾佰仟]+)"
    r"\s*(万元|亿元|元|万)?"
)


def _blob(*parts: Any) -> str:
    return " ".join(str(p or "") for p in parts)


def _is_payment_suspect(sus: dict[str, Any]) -> bool:
    # 门禁 P2-1：价值判断类永不按「价款再核对」机器放行（总原则：价值判断
    # → 人）——旧实现只看「价款」字样，违约金/赔偿类摘句挂在价款项下会被
    # verified 静默。
    _VALUE_JUDGMENT_KEYS = ("违约金", "赔偿", "损害", "责任", "风险", "过高", "偏高", "不利")
    ref = str(sus.get("source_ref") or "")
    title = str(sus.get("title") or "")
    question = str(sus.get("question") or "")
    blob = _blob(ref, title, question)
    if any(k in blob for k in _VALUE_JUDGMENT_KEYS):
        return False
    if any(k in ref for k in _PAYMENT_IDS) or any(k in title for k in _PAYMENT_IDS):
        return True
    if sus.get("source") == "rule_attention" and any(k in blob for k in _PAYMENT_NAME_KEYS):
        return True
    if "价款" in blob:
        return True
    if "再核对" in blob and any(k in blob for k in _PAYMENT_NAME_KEYS):
        return True
    return False


def _amount_values_in_text(text: str) -> list[str]:
    """抽出金额字面量（保序去重），用于跨条款一致性粗判。"""
    found: list[str] = []
    seen: set[str] = set()
    for m in _AMOUNT_NORM_RE.finditer(text or ""):
        raw = m.group(0).strip()
        # 归一：去空白
        key = "".join(raw.split())
        if key and key not in seen:
            seen.add(key)
            found.append(key)
    return found


def _amount_facts_consistent(
    facts: list[dict[str, Any]], text: str
) -> tuple[bool, str]:
    """金额跨条款：完全一致且可追原文 → True；否则 False + 原因。"""
    amount_facts = [
        f for f in (facts or [])
        if isinstance(f, dict) and str(f.get("kind") or "") == "amount"
    ]
    values: list[str] = []
    for f in amount_facts:
        val = "".join(str(f.get("value") or "").split())
        if not val:
            continue
        ev = f.get("evidence") if isinstance(f.get("evidence"), dict) else {}
        ver = (ev or {}).get("verification") or "unverified"
        if ver != "verified" and not quote_supported(text or "", str(f.get("value") or "")):
            return False, "金额事实无法追原文"
        values.append(val)
    # 无金额事实时，用正文金额字面量兜底
    if not values:
        values = _amount_values_in_text(text or "")
        if len(values) <= 1:
            return True, "金额唯一或未检出"
        # 多个不同字面量 → 不一致
        if len(set(values)) == 1:
            return True, "正文金额字面量一致"
        return False, "正文金额字面量不一致"
    if len(set(values)) == 1:
        return True, "金额事实完全一致且可追原文"
    return False, "金额事实不一致"


def classify_triage(
    sus: dict[str, Any],
    *,
    verification: str,
    fact_ok: Optional[bool],
    text: str,
    facts: Optional[list[dict[str, Any]]] = None,
) -> tuple[TriageDisposition, str, str]:
    """法务五条 + 总原则 → (disposition, rule_id, reason)。

    优先级：异议期(1) → 验收另附(2) → 价款再核对(3) → 摘句未定位(4) → 金额跨条款(5)
    → 默认（证据闭合无价值判断可静默，否则人审）。
    说明：①②③是样例业务标签；④只在「有摘句但对不上原文」时触发。
    """
    ver = verification or "unverified"
    title = str(sus.get("title") or "")
    question = str(sus.get("question") or "")
    blob = _blob(title, question, sus.get("source"), sus.get("source_ref"))
    source = str(sus.get("source") or "")
    quote = str(sus.get("quote") or "").strip()

    # 1) 异议期偏短 → 必须人审（商业取舍）
    if "异议期" in blob or (
        any(k in blob for k in _OBJECTION_KEYS) and "偏短" in blob
    ):
        return "must_human", "objection_short", "异议期涉及商业取舍，必须人审"

    # 2) 验收标准是否另附 → 必须人审（另约可能）
    if any(k in blob for k in _ACCEPTANCE_KEYS):
        return "must_human", "acceptance_annex", "验收标准是否另附涉及另约可能，必须人审"

    # 3) 价款再核对 → verified 可机器放行，否则升人审
    if _is_payment_suspect(sus):
        if ver == "verified":
            return "machine_ok", "payment_recheck", "价款摘句已核对 verified，机器放行"
        return "must_human", "payment_recheck", f"价款再核对 verification={ver}，升人审"

    # 4) 摘句未定位 → 必须人审（有摘句但对不上原文 = 证据断裂）
    if quote and ver == "missing":
        return "must_human", "quote_unlocated", "摘句未定位，证据断裂须人审"

    # 5) 金额跨条款
    # 一致→静默接受 ambiguous：同额摘句在多条款出现是多位置命中，正是跨条款
    # 对照的常态；一致性判断交由 _amount_facts_consistent 负责（门禁整改）
    if source == "fact" and (
        str(sus.get("title") or "") in {"金额", "价款"}
        or "金额" in blob
        or "kind:amount" in blob
    ):
        ok, why = _amount_facts_consistent(facts or [], text or "")
        if ok and ver in {"verified", "ambiguous"} and fact_ok is not False:
            return "machine_silent", "amount_cross", f"金额跨条款一致可静默（{why}）"
        return "must_human", "amount_cross", f"金额跨条款须人审（{why}）"
    if "金额" in blob and ("跨" in blob or "一致" in blob or source == "fact"):
        ok, why = _amount_facts_consistent(facts or [], text or "")
        if ok and ver in {"verified", "ambiguous"} and fact_ok is not False:
            return "machine_silent", "amount_cross", f"金额跨条款一致可静默（{why}）"
        return "must_human", "amount_cross", f"金额跨条款须人审（{why}）"

    # 默认总原则：证据链闭合且无价值判断 → 机器静默；否则人审
    if ver == "verified" and source in {"fact"} and fact_ok is True:
        return "machine_silent", "default", "证据链闭合且事实一致，机器静默"
    if ver == "verified" and source in {"rule_attention"} and not any(
        k in blob for k in ("偏短", "另附", "是否", "建议")
    ):
        return "machine_ok", "default", "规则摘句已核对且无价值判断，机器放行"
    return "must_human", "default", "默认进人审（含价值判断或证据未闭合）"


def _collect_suspects(
    *,
    items: list[dict[str, Any]],
    quality: Optional[dict[str, Any]],
    blind_candidates: list[dict[str, Any]],
    facts: list[dict[str, Any]],
    max_questions: int,
) -> list[dict[str, Any]]:
    """从审查产物收集疑点（封顶；不改动入参）。"""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()

    def _add(
        *,
        source: str,
        source_ref: str,
        question: str,
        title: str = "",
        clause_id: Optional[str] = None,
        quote: str = "",
        parse_source: str = "quality",
        value_for_fact: str = "",
        source_subject_key: str = "",
    ) -> None:
        if len(out) >= max_questions:
            return
        key = f"{source}|{source_ref}|{(quote or question)[:40]}"
        if key in seen:
            return
        seen.add(key)
        out.append(
            {
                "source": source,
                "source_ref": source_ref,
                # 批 2b-②：来源对象稳定键（入 claim 身份；source_ref 已降级纯展示）
                "source_subject_key": source_subject_key,
                "question": question,
                "title": title,
                "clause_id": clause_id,
                "quote": quote,
                "parse_source": parse_source,
                "value_for_fact": value_for_fact,
            }
        )

    q = quality or {}
    for i, obs in enumerate(q.get("observations") or []):
        if not isinstance(obs, dict):
            continue
        title = str(obs.get("title") or "").strip()
        comment = str(obs.get("comment") or "").strip()
        quote = str(obs.get("quote") or "").strip()
        cid = obs.get("clause_id")
        qtext = f"请确认：「{title}」是否属实？" if title else "请确认这条 AI 观察是否属实？"
        if comment:
            qtext = f"请确认：「{title}」——{comment[:60]}"
        _add(
            source="quality_obs",
            # 批 2b-②：来源对象键 = dimension（证据 ID 已是独立键成分）
            source_subject_key=str(obs.get("dimension") or ""),
            source_ref=f"obs:{i}",
            question=qtext[:120],
            title=title,
            clause_id=str(cid) if cid else None,
            quote=quote,
            parse_source="quality",
        )

    for pq in q.get("pending_questions") or []:
        if isinstance(pq, str) and pq.strip():
            _add(
                source="pending",
                source_ref=f"pending:{pq.strip()[:24]}",
                question=pq.strip()[:120],
                title="待核实",
            )
        elif isinstance(pq, dict):
            qt = str(pq.get("question") or pq.get("text") or "").strip()
            if qt:
                _add(
                    source="pending",
                    source_ref=f"pending:{qt[:24]}",
                    question=qt[:120],
                    title="待核实",
                    clause_id=str(pq.get("clause_id")) if pq.get("clause_id") else None,
                    quote=str(pq.get("quote") or ""),
                )

    for bc in blind_candidates or []:
        if not isinstance(bc, dict):
            continue
        name = str(bc.get("name") or bc.get("id") or "候选").strip()
        quote = str(bc.get("quote") or "").strip()
        cid = bc.get("primary_clause_id") or (
            (bc.get("clause_ids") or [None])[0]
        )
        _add(
            source="blind",
            source_subject_key=str(bc.get("id") or ""),
            source_ref=str(bc.get("id") or name),
            question=f"请确认候选风险「{name}」是否需要跟进？",
            title=name,
            clause_id=str(cid) if cid else None,
            quote=quote,
            parse_source="blind",
        )

    for i, fact in enumerate(facts or []):
        if not isinstance(fact, dict):
            continue
        ev = fact.get("evidence") if isinstance(fact.get("evidence"), dict) else {}
        ver = (ev or {}).get("verification") or "unverified"
        label = str(fact.get("label") or fact.get("kind") or "事实").strip()
        value = str(fact.get("value") or "").strip()
        if ver in {"verified"} and (ev or {}).get("clause_id"):
            # 已核过的确定性事实不刷屏——但**金额**除外（门禁 P1-1）：
            # 五条⑤「金额跨条款一致才静默」恰恰要求对已核实事实做跨条款
            # 复核，矛盾发生在两个各自真实的事实之间（总价十万 vs 结算八万）。
            is_amount = str(fact.get("kind") or "") == "amount" or "金额" in label
            if not is_amount:
                continue
        quote = str((ev or {}).get("quote") or value)
        _add(
            source="fact",
            # 批 2b-②：来源对象键 = kind+value 规范化业务键哈希（禁下标/禁截断）
            source_subject_key="fact:" + hashlib.sha256(
                (str(fact.get("kind") or "") + chr(31) + value).encode("utf-8")
            ).hexdigest()[:12],
            source_ref=f"fact:{i}:{value[:20]}",
            question=f"请核对事实材料「{label}：{value}」是否与原文一致？",
            title=label,
            clause_id=(ev or {}).get("clause_id"),
            quote=quote,
            parse_source="fact",
            value_for_fact=value,
        )

    # 规则「需关注」作次级疑点（仅填空位；不暗示改档）
    for item in items or []:
        if len(out) >= max_questions:
            break
        if not isinstance(item, dict) or item.get("status") != "需关注":
            continue
        name = str(item.get("name") or item.get("id") or "").strip()
        quote = str(item.get("quote") or "").strip()
        cid = item.get("primary_clause_id") or ((item.get("clause_ids") or [None])[0])
        _add(
            source="rule_attention",
            source_subject_key=str(item.get("id") or ""),
            source_ref=str(item.get("id") or name),
            question=f"请确认规则项「{name}」的摘句与说明是否对得上原文？",
            title=name,
            clause_id=str(cid) if cid else None,
            quote=quote,
            parse_source="rules",
        )

    return out[:max_questions]


def run_bounded_verify(
    *,
    text: str,
    items: Optional[list[dict[str, Any]]] = None,
    quality: Optional[dict[str, Any]] = None,
    blind_candidates: Optional[list[dict[str, Any]]] = None,
    facts: Optional[list[dict[str, Any]]] = None,
    clause_index: Optional[dict[str, Any]] = None,
    document_version: str = "",
    prior: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """跑一轮有界核验：取证 → 核对 → 产出待确认问题。不碰规则 items status。"""
    lim = limits_from_env()
    doc_ver = document_version or document_version_for(text or "")
    prior = prior or {}
    rounds_used = int(prior.get("rounds_used") or 0)
    fetches_used = int(prior.get("clause_fetches_used") or 0)

    if not (text or "").strip():
        return empty_verify(reason=Reason.ERROR.value, document_version=doc_ver)

    if rounds_used >= lim["max_rounds"]:
        # 保留既有问题，标预算耗尽
        kept = prior.get("questions") or []
        info = VerifyInfo(
            available=True,
            reason=Reason.BUDGET_EXCEEDED.value,
            rounds_used=rounds_used,
            max_rounds=lim["max_rounds"],
            clause_fetches_used=fetches_used,
            max_clause_fetches=lim["max_clause_fetches"],
            max_questions=lim["max_questions"],
            max_recheck_per_question=lim["max_recheck_per_question"],
            questions=[ConfirmQuestion(**q) for q in kept if isinstance(q, dict)],
            document_version=doc_ver,
            triage_log=list(prior.get("triage_log") or []),
        )
        return info.model_dump()

    # 若已有未确认问题且 prior 已 available：本轮视为「再跑」只增量，但仍计 round
    existing_qs = [
        ConfirmQuestion(**q) for q in (prior.get("questions") or []) if isinstance(q, dict)
    ]
    # 已确认/争议的保留；pending 本轮重建（同 source_ref 去重）
    retained = [q for q in existing_qs if q.status != "pending"]
    retained_refs = {q.source_ref for q in retained}

    facts_list = facts or []
    if not facts_list and isinstance(quality, dict):
        facts_list = quality.get("facts") or []

    suspects = _collect_suspects(
        items=items or [],
        quality=quality,
        blind_candidates=blind_candidates or [],
        facts=facts_list,
        max_questions=lim["max_questions"],
    )

    questions: list[ConfirmQuestion] = list(retained)
    room = lim["max_questions"] - len(questions)
    seq = len(questions)
    triage_log: list[dict[str, Any]] = list(prior.get("triage_log") or [])

    for sus in suspects:
        if sus["source_ref"] in retained_refs:
            continue
        # room=0 时仍跑分流记账（machine_*），只是不再追加 must_human 待办
        if fetches_used >= lim["max_clause_fetches"] and sus.get("clause_id"):
            # 预算紧时仍可产出无取证的问题，但不再按条款取正文
            clause = None
        else:
            clause = None
            if sus.get("clause_id"):
                clause = clause_by_id(text, clause_index, sus.get("clause_id"))
                if clause is not None:
                    fetches_used += 1

        quote = (sus.get("quote") or "").strip()
        # 若嫌疑 clause_id 取证失败，改由摘句坐标派生（不信任模型错号）
        resolved_cid = (clause or {}).get("id") if clause else None
        check_text = (clause or {}).get("text") if clause else text
        ps = sus.get("parse_source") or "quality"
        if quote and check_text:
            if quote_supported(check_text, quote) or quote_supported(text, quote):
                # 门禁 P2-2：摘句不在声称条款内而由全文兜底命中时，不把声称的
                # clause_id 递给证据——让票据按真实坐标派生归属（F06 同向：
                # 「引用在全文里」≠「引用在声称的条款里」）
                in_claimed = clause is not None and quote_supported(check_text, quote)
                evidence = verify_quote_against_text(
                    text,
                    quote,
                    document_version=doc_ver,
                    clause_id=resolved_cid if in_claimed else None,
                    clause_index=clause_index,
                    parse_source=ps,
                )
            else:
                evidence = build_evidence(
                    text=text,
                    quote=quote[:MAX_QUOTE_CHARS],
                    parse_source=ps,  # type: ignore[arg-type]
                    document_version=doc_ver,
                    clause_id=resolved_cid,
                    force_verification="missing",
                )
        elif quote:
            evidence = verify_quote_against_text(
                text,
                quote,
                document_version=doc_ver,
                clause_id=resolved_cid,
                clause_index=clause_index,
                parse_source=ps,
            )
        else:
            evidence = build_evidence(
                text=text,
                quote="",
                parse_source=ps,  # type: ignore[arg-type]
                document_version=doc_ver,
                clause_id=resolved_cid,
                force_verification="missing",
            )
        # 取证失败时，用证据坐标回填 clause_id；坐标命中后再补一次取证（计预算）
        if not resolved_cid and evidence.get("clause_id"):
            resolved_cid = evidence.get("clause_id")
            if fetches_used < lim["max_clause_fetches"]:
                clause2 = clause_by_id(text, clause_index, resolved_cid)
                if clause2 is not None:
                    fetches_used += 1
                    clause = clause2

        # 事实核对：value 是否出现在原文
        fact_ok: Optional[bool] = None
        if sus["source"] == "fact":
            val = (sus.get("value_for_fact") or quote or "").strip()
            fact_ok = bool(val) and quote_supported(text, val)

        ver = (evidence or {}).get("verification") or "unverified"
        # 金额 fact：把 kind 塞进 blob 便于五条⑤命中
        if sus["source"] == "fact" and "金额" in str(sus.get("title") or ""):
            sus = {**sus, "source_ref": f"{sus.get('source_ref')}|kind:amount"}

        disposition, rule_id, reason = classify_triage(
            sus,
            verification=ver,
            fact_ok=fact_ok,
            text=text,
            facts=facts_list,
        )
        seq += 1
        qid = f"vq{seq:02d}"
        q_payload = dict(
            id=qid,
            source=sus["source"],
            source_subject_key=str(sus.get("source_subject_key") or ""),
            source_ref=sus["source_ref"],
            question=sus["question"],
            title=sus.get("title") or "",
            clause_id=(evidence or {}).get("clause_id") or sus.get("clause_id"),
            quote=(evidence or {}).get("quote") or quote[:MAX_QUOTE_CHARS],
            evidence=evidence,
            verification=ver,
            fact_ok=fact_ok,
            status="pending",
            triage=disposition,
            triage_reason=reason,
            triage_rule=rule_id,
        )
        triage_log.append(
            {
                "id": qid,
                "source": sus["source"],
                "source_ref": sus["source_ref"],
                "title": q_payload["title"],
                "triage": disposition,
                "triage_rule": rule_id,
                "triage_reason": reason,
                "verification": ver,
            }
        )
        # 仅 must_human 进入「需你确认」待办；机器放行/静默不打扰人（Design B 仍不改规则档）
        if disposition == "must_human" and room > 0:
            questions.append(ConfirmQuestion.model_validate(q_payload))
            room -= 1
        # machine_ok / machine_silent：只记 triage_log，不占 pending 名额

    rounds_used += 1
    info = VerifyInfo(
        available=True,
        reason=None if questions else ("empty" if not triage_log else "triaged_clear"),
        rounds_used=rounds_used,
        max_rounds=lim["max_rounds"],
        clause_fetches_used=fetches_used,
        max_clause_fetches=lim["max_clause_fetches"],
        max_questions=lim["max_questions"],
        max_recheck_per_question=lim["max_recheck_per_question"],
        questions=questions[: lim["max_questions"]],
        document_version=doc_ver,
        triage_log=triage_log,
    )
    return info.model_dump()


def apply_confirmation(
    verify_state: dict[str, Any],
    *,
    question_id: str,
    choice: ConfirmChoice,
    human_note: str = "",
    revised_quote: str = "",
) -> dict[str, Any]:
    """人工确认：只改 verify.questions，绝不碰规则 items。"""
    if not verify_state or not verify_state.get("available"):
        raise ValueError("核验尚未就绪")
    questions = list(verify_state.get("questions") or [])
    found = False
    for i, q in enumerate(questions):
        if not isinstance(q, dict) or q.get("id") != question_id:
            continue
        found = True
        status: QuestionStatus = "confirmed" if choice == "confirm" else "disputed"
        updated = {
            **q,
            "status": status,
            "human_choice": choice,
            "human_note": (human_note or "")[:300],
            "revised_quote": (revised_quote or "")[:MAX_QUOTE_CHARS],
        }
        questions[i] = updated
        break
    if not found:
        raise KeyError(question_id)
    out = {**verify_state, "questions": questions}
    return out


def recheck_question(
    *,
    text: str,
    verify_state: dict[str, Any],
    question_id: str,
    clause_index: Optional[dict[str, Any]] = None,
    items_snapshot: Optional[list[dict[str, Any]]] = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """确认后的再核：用修订摘句/原文再跑一次证据定位。

    返回 (新 verify_state, items_snapshot 原样回传——Design B：调用方应用它
    断言档位未变；本函数绝不修改 items)。
    """
    # Design B：显式不碰 items；只读快照用于调用方回归
    _ = items_snapshot

    lim = limits_from_env()
    max_re = int(
        verify_state.get("max_recheck_per_question")
        or lim["max_recheck_per_question"]
    )
    rounds_used = int(verify_state.get("rounds_used") or 0)
    fetches_used = int(verify_state.get("clause_fetches_used") or 0)
    max_rounds = int(verify_state.get("max_rounds") or lim["max_rounds"])
    max_fetches = int(verify_state.get("max_clause_fetches") or lim["max_clause_fetches"])
    doc_ver = verify_state.get("document_version") or document_version_for(text or "")

    if rounds_used >= max_rounds:
        raise RuntimeError("budget_exceeded")

    questions = list(verify_state.get("questions") or [])
    idx = next(
        (i for i, q in enumerate(questions) if isinstance(q, dict) and q.get("id") == question_id),
        -1,
    )
    if idx < 0:
        raise KeyError(question_id)
    q = dict(questions[idx])
    if q.get("status") not in {"confirmed", "disputed", "rechecked"}:
        raise ValueError("请先提交确认，再发起再核")
    if int(q.get("recheck_count") or 0) >= max_re:
        raise RuntimeError("recheck_budget_exceeded")

    quote = (q.get("revised_quote") or q.get("quote") or "").strip()
    cid = q.get("clause_id")
    clause = None
    if cid and fetches_used < max_fetches:
        clause = clause_by_id(text, clause_index, cid)
        if clause is not None:
            fetches_used += 1

    check_text = (clause or {}).get("text") if clause else text
    if quote and check_text and (
        quote_supported(check_text, quote) or quote_supported(text or "", quote)
    ):
        evidence = verify_quote_against_text(
            text or "",
            quote,
            document_version=doc_ver,
            clause_id=(clause or {}).get("id") or cid,
            clause_index=clause_index,
            parse_source="quality",
        )
    else:
        _, _, loc = locate_quote_span(text or "", quote)
        evidence = build_evidence(
            text=text or "",
            quote=quote[:MAX_QUOTE_CHARS],
            parse_source="quality",
            document_version=doc_ver,
            clause_id=cid,
            force_verification=loc if quote else "missing",  # type: ignore[arg-type]
        )

    fact_ok = q.get("fact_ok")
    if q.get("source") == "fact" and quote:
        fact_ok = quote_supported(text or "", quote)

    recheck = {
        "verification": evidence.get("verification"),
        "evidence": evidence,
        "fact_ok": fact_ok,
        "clause_fetched": bool(clause),
    }
    q.update(
        {
            "status": "rechecked",
            "evidence": evidence,
            "verification": evidence.get("verification") or "unverified",
            "quote": evidence.get("quote") or quote[:MAX_QUOTE_CHARS],
            "fact_ok": fact_ok,
            "recheck_count": int(q.get("recheck_count") or 0) + 1,
            "last_recheck": recheck,
        }
    )
    questions[idx] = q
    rounds_used += 1
    out = {
        **verify_state,
        "rounds_used": rounds_used,
        "clause_fetches_used": fetches_used,
        "questions": questions,
        "document_version": doc_ver,
        "available": True,
        "reason": None,
    }
    # 原样返回 items 快照供回归（本函数未修改）
    return out, list(items_snapshot or [])


# 进程内轻量锁：同 review 并发 confirm/recheck 串行化（store 自身有锁，这里防读改写竞态窗口）
_review_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def lock_for(review_id: str) -> threading.Lock:
    with _locks_guard:
        lk = _review_locks.get(review_id)
        if lk is None:
            lk = threading.Lock()
            _review_locks[review_id] = lk
        return lk
