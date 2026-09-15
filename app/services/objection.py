"""异议层（阶段 3.1/3.2）：LLM 对 heuristic/existence 规则结果提异议候选。

铁律 3（路线图宪章）：异议**永不直接变更档位**——本模块输出只进 store 的
objections 键，结构上不回流 items/scorecard（test_objection.py 用深度相等
硬断言钉死，同 quality 先例）。采纳异议的人工动作产物=规则变更提案文本
（复制给人评审），不是当次改判。

受理分流（代码级，送审前拦截，省预算且缩攻击面；矩阵见
docs/stage3-class-opinion.md 第五节）：
- rule_class=hardline → 不送 LLM（hardline 只解释，永不接受异议）；
- heuristic + 需关注（有命中规则）→ 只收 false_positive（误报）；
- existence + 未找到 / missing_as 缺项档位（需关注且无命中规则）→
  只收 omission（漏报，等价写法被词表漏掉）；
- 其余组合（通过/NA 等）不产生候选。
运行时三道防线：候选分流拦截 → 送审候选集合比对（候选外申报静默丢弃）
→ 方向×类别匹配拒收。

五要件缺一不受理（服务端硬校验，缺任一记 unaccepted+reason）：
① quote 全文校验+条款定位（F06 教训：不信任模型给的编号）
② counter_evidence 非空（「未发现反证原文」声明合法）
③ legal_reasoning ≥ 30 字
④ stance_check 与当前立场一致或「与立场无关」
⑤ 不改变档位声明：代码强制（不在模型输出 schema，同 needs_confirm 先例）

失败一律软降级（ObjectionInfo.available=False + reason 码），绝不阻断主链。
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Callable, Optional

from pydantic import BaseModel, Field

from app.prompts import objection as objection_prompts
from app.services import blind_spot, llm_ask, scorecard
from app.services.clause_index import locate_quote_clauses

logger = logging.getLogger(__name__)

ChatFn = Callable[[str, str], str]

SYSTEM_MARKER = objection_prompts.SYSTEM_MARKER

MAX_OBJECTIONS = 6
MAX_REASONING_CHARS = 300
MIN_REASONING_CHARS = 30
MAX_QUOTE_CHARS = 200
MIN_QUOTE_CHARS = 6  # 与 blind_spot 压缩容差档一致：单字/碎片摘句不受理
_DECLARATION = "未发现反证原文"

_DIRECTIONS = ("false_positive", "omission")


class Objection(BaseModel):
    item_id: str
    rule_id: Optional[str] = None
    rule_class: str = "heuristic"  # hardline / existence / heuristic
    direction: str  # false_positive / omission
    quote: str = ""
    counter_evidence: str = ""
    legal_reasoning: str = ""
    stance_check: str = ""
    proposal: str = ""
    accepted: bool = False  # 五要件全过才 True；缺任一为 False + reject_reason
    reject_reason: Optional[str] = None
    clause_id: Optional[str] = None  # 服务端按 quote 定位（不信任模型编号）
    clause_ambiguous: bool = False  # 摘句跨多条款时 True
    adopted: bool = False  # 人工「采纳为规则改进提案」动作（只改本键）
    needs_confirm: bool = True  # 代码强制；模型输出 schema 里没有此字段


class ObjectionInfo(BaseModel):
    available: bool = False
    # disabled / no_llm_key / llm_error / parse_failed / budget_exceeded / error
    reason: Optional[str] = None
    objections: list[Objection] = Field(default_factory=list)
    rejected_count: int = 0  # 五要件拒收数（观测用，前端不渲染）
    disclaimer: str = "异议只是候选线索，不改变逐条核查结论；是否成立由人工与规则修订决定。"


def is_objections_enabled() -> bool:
    """默认开；conftest autouse 关闭保测试确定性（同 quality/precheck 三件套）。"""
    return os.getenv("OBJECTIONS_ENABLED", "true").strip().lower() not in {
        "false",
        "0",
        "no",
    }


def outcome_unavailable(reason: str) -> dict[str, Any]:
    return ObjectionInfo(available=False, reason=reason).model_dump()


def _default_chat_fn() -> Optional[ChatFn]:
    timeout = float(os.getenv("OBJECTION_TIMEOUT_SECONDS", "") or os.getenv("LLM_TIMEOUT_SECONDS", "180") or 180)
    deepseek = llm_ask._deepseek_key()
    zhipu = llm_ask._zhipu_key()
    xai = llm_ask._xai_key()
    if deepseek:
        return lambda s, u: llm_ask._chat_deepseek(deepseek, s, u, timeout=timeout)
    if zhipu:
        return lambda s, u: llm_ask._chat_zhipu(zhipu, s, u, timeout=timeout)
    if xai:
        return lambda s, u: llm_ask._chat_xai(xai, s, u, timeout=timeout)
    return None


def _collect_candidates(
    items: list[dict[str, Any]], max_candidates: int
) -> list[dict[str, Any]]:
    """按三分法分流候选（送审前拦截，裁定书 stage3-class-opinion.md 第五节矩阵）：
    heuristic+需关注（有命中）→ 误报候选；
    existence+未找到 / existence+missing_as 缺项档位（需关注且无命中规则）→ 漏报候选；
    hardline 与其余组合（含 existence+通过：词表已认出无「缺」可漏）不送审。
    missing 落点以 rule_id is None 识别（checklist 引擎保证该路径不产生 rule_id）。"""
    out: list[dict[str, Any]] = []
    for it in items or []:
        if not isinstance(it, dict) or it.get("category_na"):
            continue
        rc = str(it.get("rule_class") or "heuristic")
        st = it.get("status")
        has_hit = bool(it.get("rule_id"))  # 命中具体规则才算「标了」；None=missing 落点
        direction: Optional[str] = None
        if rc == "heuristic" and st == "需关注" and has_hit:
            direction = "false_positive"
        elif rc == "existence" and (st == "未找到" or (st == "需关注" and not has_hit)):
            direction = "omission"
        if not direction:
            continue  # hardline 与其余组合：不送审
        out.append(
            {
                "item_id": str(it.get("id") or ""),
                "rule_id": it.get("rule_id"),
                "rule_class": rc,
                "direction": direction,
                "name": str(it.get("name") or ""),
                "note": str(it.get("note") or ""),
                "quote": str(it.get("quote") or ""),
            }
        )
        if len(out) >= max_candidates:
            break
    return out


def _candidates_block(candidates: list[dict[str, Any]], text: str, clause_index: Optional[dict]) -> str:
    clauses_by_id = {
        str(c.get("id")): c for c in ((clause_index or {}).get("clauses") or []) if c.get("id")
    }
    lines: list[str] = []
    for c in candidates:
        related = ""
        if c.get("quote"):
            ids = locate_quote_clauses(text, c["quote"], clause_index or {})
            parts = []
            for cid in ids[:2]:
                cl = clauses_by_id.get(cid) or {}
                parts.append(f"【{cid} {str(cl.get('heading') or '')[:30]}】")
            related = "；".join(parts)
        lines.append(
            f"- 条目 {c['item_id']}（{c['name']}）｜规则结论：{'需关注' if c['direction']=='false_positive' else '未找到'}"
            f"｜规则备注：{c['note'] or '无'}｜规则摘句：{c['quote'] or '无'}"
            f"｜相关条款：{related or '未定位到，请跳过该条'}"
        )
    return "\n".join(lines)


def _parse_objections(raw: str) -> Optional[list[dict[str, Any]]]:
    if not raw:
        return None
    text = raw.strip()
    fence = re.match(r"^```(?:json)?\s*([\s\S]*?)\s*```$", text)
    if fence:
        text = fence.group(1).strip()
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{[\s\S]*\}", text)
        if not m:
            return None
        try:
            obj = json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
    obs = obj.get("objections") if isinstance(obj, dict) else None
    if not isinstance(obs, list):
        return None
    return [o for o in obs if isinstance(o, dict)]


def _has_forbidden(parsed: list[dict[str, Any]]) -> bool:
    blob = " ".join(
        " ".join(str(v) for k, v in o.items() if k != "item_id")
        for o in parsed
        if isinstance(o, dict)
    )
    return bool(scorecard.check_forbidden(blob) or llm_ask._scrub_banned_echo(blob) != blob)


def _validate(
    r: dict[str, Any],
    stance: str,
    text: str,
    clause_index: Optional[dict],
    reasoning_clean: str = "",
) -> tuple[bool, Optional[str], Optional[str], Optional[bool]]:
    """五要件硬校验。返回 (accepted, reject_reason, clause_id, clause_ambiguous)。

    reasoning_clean：禁语清洗后的法律逻辑链文本（要件③按它复验长度，
    防「先凑禁语到 30 字、洗完剩空壳」的绕过面）。
    """
    item_id = str(r.get("item_id") or "").strip()
    if not item_id:
        return False, "缺 item_id", None, None
    # ① 完整条款引用：全文校验 + 服务端定位（不信任模型编号，F06 教训）；
    # 最小长度门槛与 blind_spot 压缩容差档一致（<6 字单字/碎片摘句不受理）
    quote = str(r.get("quote") or "").strip().strip("「」\"'“”").strip()
    if len(quote) < MIN_QUOTE_CHARS or not blind_spot.quote_supported(text, quote):
        return False, "要件①条款引用未能在原文核验", None, None
    ids = locate_quote_clauses(text, quote, clause_index or {})
    if not ids:
        return False, "要件①条款引用未能定位条款", None, None
    clause_ambiguous = len(ids) > 1
    clause_id = None if clause_ambiguous else str(ids[0])
    # ② 反证引用或声明无
    counter = str(r.get("counter_evidence") or "").strip()
    if not counter:
        return False, "要件②反证缺失", clause_id, clause_ambiguous
    if counter != _DECLARATION and not blind_spot.quote_supported(text, counter):
        return False, "要件②反证原文未能在原文核验", clause_id, clause_ambiguous
    # ③ 法律逻辑链（按清洗后文本复验）
    reasoning = (reasoning_clean or str(r.get("legal_reasoning") or "").strip())
    if len(reasoning) < MIN_REASONING_CHARS:
        return False, "要件③法律逻辑链过短", clause_id, clause_ambiguous
    # ④ 立场自检
    stance_check = str(r.get("stance_check") or "").strip()
    if stance_check not in {stance, "与立场无关", "neutral"}:
        return False, f"要件④立场自检不符（{stance_check or '空'}）", clause_id, clause_ambiguous
    return True, None, clause_id, clause_ambiguous


def run_objections(
    *,
    text: str,
    items: list[dict[str, Any]],
    stance: str = "neutral",
    clause_index: Optional[dict[str, Any]] = None,
    chat_fn: Optional[ChatFn] = None,
    budget: Optional[Any] = None,
) -> dict[str, Any]:
    """异议层主入口。失败绝不 raise，失败矩阵见 ObjectionInfo.reason。"""
    if not is_objections_enabled():
        return outcome_unavailable("disabled")
    if not (text or "").strip():
        return outcome_unavailable("error")
    chat = chat_fn or _default_chat_fn()
    if chat is None:
        return outcome_unavailable("no_llm_key")

    candidates = _collect_candidates(items, max_candidates=12)
    if not candidates:
        # 没有可送审候选（如全 hardline/通过）：合法空结果
        return ObjectionInfo(available=True, reason=None, objections=[]).model_dump()

    block = _candidates_block(candidates, text, clause_index)
    catalog_lines = [
        f"- {c.get('id')} {str(c.get('heading') or '')[:30]}"
        for c in ((clause_index or {}).get("clauses") or [])
        if c.get("id")
    ]
    catalog = "\n".join(catalog_lines)

    system = objection_prompts.build_system_prompt(stance)
    user = objection_prompts.build_user_prompt(block, catalog)
    if budget is not None and not budget.try_consume():
        logger.warning("Objections skipped: LLM budget exhausted")
        return outcome_unavailable("budget_exceeded")

    def _is_bad(p: Optional[list[dict[str, Any]]]) -> bool:
        return p is None or _has_forbidden(p)

    try:
        raw = chat(system, user)
    except Exception:  # noqa: BLE001
        logger.exception("Objections LLM error")
        return outcome_unavailable("llm_error")
    parsed = _parse_objections(raw)
    if _is_bad(parsed):
        if budget is not None and not budget.try_consume():
            return outcome_unavailable("budget_exceeded")
        try:
            raw = chat(objection_prompts.build_retry_system_prompt(system), user)
        except Exception:  # noqa: BLE001
            logger.exception("Objections retry LLM error")
            return outcome_unavailable("llm_error")
        parsed = _parse_objections(raw)
        if _is_bad(parsed):
            # 重试后仍解析失败或带禁语：整单软降级（防线对称，不给「二轮洗白」口）
            return outcome_unavailable("parse_failed")

    objections: list[Objection] = []
    rejected = 0
    valid_by_id = {str(it.get("id")): it for it in items or [] if isinstance(it, dict)}
    # 送审候选集合（第二道防线）：候选外的 (item_id, direction) 一律静默丢弃
    # （模型幻觉/注入的未送审组合，如 existence+需关注+omission 的洗白方向——
    # 连 rejected 都不计：结构性错误不是可展示的候选）
    sent = {(str(c["item_id"]), str(c["direction"])) for c in candidates}
    seen_refs: set[str] = set()
    for r in parsed:
        if not isinstance(r, dict) or len(objections) >= MAX_OBJECTIONS:
            break
        item_id = str(r.get("item_id") or "")
        direction = str(r.get("direction") or "")
        if (item_id, direction) not in sent:
            continue  # 候选外申报：静默丢弃，不留痕
        it = valid_by_id.get(item_id) or {}
        rc = str(it.get("rule_class") or "heuristic")
        # 方向×类别匹配（与候选分流同矩阵，三重保险的第三道）
        expected = "false_positive" if rc == "heuristic" else "omission" if rc == "existence" else None
        if expected is None or direction != expected:
            rejected += 1
            continue
        # 禁语清洗先行：要件③对清洗后的文本复验长度（先凑禁语到 30 字再洗空的绕过面）
        reasoning = llm_ask._scrub_banned_echo(scorecard.scrub_forbidden(
            str(r.get("legal_reasoning") or ""))).strip()[:MAX_REASONING_CHARS]
        accepted, why, clause_id, ambiguous = _validate(
            r, stance, text, clause_index, reasoning_clean=reasoning)
        if not accepted:
            rejected += 1
        ref = f"{item_id}|{direction}|{(r.get('quote') or '')[:40]}"
        if ref in seen_refs:
            continue
        seen_refs.add(ref)
        proposal = llm_ask._scrub_banned_echo(scorecard.scrub_forbidden(
            str(r.get("proposal") or ""))).strip()
        rid = r.get("rule_id") or it.get("rule_id")
        objections.append(
            Objection(
                item_id=item_id,
                rule_id=str(rid) if rid else None,
                rule_class=rc,
                direction=direction,
                quote=str(r.get("quote") or "")[:MAX_QUOTE_CHARS],
                counter_evidence=str(r.get("counter_evidence") or "")[:MAX_QUOTE_CHARS],
                legal_reasoning=reasoning,
                stance_check=str(r.get("stance_check") or ""),
                proposal=proposal,
                accepted=accepted,
                reject_reason=why,
                clause_id=clause_id,
                clause_ambiguous=bool(ambiguous),
                needs_confirm=True,  # 代码强制
            )
        )

    return ObjectionInfo(
        available=True,
        reason=None,
        objections=objections,
        rejected_count=rejected,
    ).model_dump()
