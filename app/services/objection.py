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
from app.services.clause_index import _find_compressed, locate_quote_clauses

logger = logging.getLogger(__name__)

ChatFn = Callable[[str, str], str]

SYSTEM_MARKER = objection_prompts.SYSTEM_MARKER

MAX_OBJECTIONS = 6
MAX_CANDIDATES = 12  # 单次送审候选上限（coverage.candidate_limited 记账）
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
    # 覆盖计账（外审批 2）：「最多送 12 个候选」等截断行为不再无痕——
    # eligible=全合同符合受理矩阵的候选总数；sent=实际送审数（≤MAX_CANDIDATES）；
    # reviewed=模型本轮返回并被逐条处理的条数；candidate_limited=候选因送审上限被截
    coverage: Optional[dict[str, Any]] = None


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


def _eligible_direction(item: dict[str, Any]) -> Optional[str]:
    """受理矩阵谓词（裁定书第五节的唯一权威实现）：
    返回该条目的异议方向（false_positive/omission）或 None（不送审）。
    _collect_candidates 与 _eligible_count 都走这里——杜绝双实现漂移
    （三轮审计 G 项）。"""
    rc = str(item.get("rule_class") or "heuristic")
    st = item.get("status")
    has_hit = bool(item.get("rule_id"))  # 命中具体规则才算「标了」；None=missing 落点
    if rc == "heuristic" and st == "需关注" and has_hit:
        return "false_positive"
    if rc == "existence" and (st == "未找到" or (st == "需关注" and not has_hit)):
        return "omission"
    return None  # hardline 与其余组合（含 existence+通过）：不送审


def _collect_candidates(
    items: list[dict[str, Any]], max_candidates: int
) -> list[dict[str, Any]]:
    """按三分法分流候选（送审前拦截）：谓词见 _eligible_direction。
    missing 落点以 rule_id is None 识别（checklist 引擎保证该路径不产生 rule_id）。"""
    out: list[dict[str, Any]] = []
    for it in items or []:
        if not isinstance(it, dict) or it.get("category_na"):
            continue
        direction = _eligible_direction(it)
        rc = str(it.get("rule_class") or "heuristic")
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
                "status": str(it.get("status") or ""),  # 透传真实档位给 LLM 上下文
                # 证据相关性范围（三轮审计 C 项收窄）：误报候选只认
                # primary_clause_id——MatchEvidence 真正触发风险的「罪案现场」；
                # clause_ids 是同关键词的全部出现位置，可作上下文不可作核心证据
                # （同词出现在 5 个条款时，拿别处约定替真实现场作证=范围过宽）。
                # 漏报候选此处为 None，由 run_objections 按「送出的正文 span」绑定
                "scope": (
                    {str(it["primary_clause_id"])}
                    if it.get("primary_clause_id") else None
                ) if direction == "false_positive" else None,
            }
        )
        if len(out) >= max_candidates:
            break
    return out


# 正文供给（外审 P1-1）：omission 方向必须让模型看得到合同正文——
# 「规则没找到」的等价写法藏在任何条款里，只给目录等于让学生改漏判的卷子
# 却不给卷子。短合同全文；长合同按条款顺序送出预算内正文并标注截断。
BODY_FULL_LIMIT = 6000  # 与分段阅读阈值一致：以内正文直送全文
BODY_CHAR_BUDGET = 16000  # 长合同正文块字符预算（超出按条款截断）


def _body_block(text: str, clause_index: Optional[dict]) -> tuple[str, dict[str, Any]]:
    """构造合同正文块。返回 (正文文本, body_info)。

    body_info（三轮审计 E/D 项升级）：
    - spans: [(start, end, clause_id), ...] 实际发送给模型的**字符区间**——
      证据相关性从「条款集合」升级为「span 精确绑定」：截断条款的未发送
      尾部不得成为 accepted evidence（引用位置必须落在模型实际看过的区间
      内）；clause_id 由包含引用位置的 span 派生（不再信任模型编号）；
    - clauses_total / clauses_sent / chars_total / chars_sent / body_limited：
      document 维覆盖账目（「候选没截断」≠「全文读完」，两维分开记）。
    """
    text = text or ""
    clauses = [
        c for c in ((clause_index or {}).get("clauses") or [])
        if c.get("id") and isinstance(c.get("start"), int) and isinstance(c.get("end"), int)
        and c["start"] < c["end"]  # end 缺失/非法会 KeyError 或吞掉剩余全文（门禁 P3-1）
    ]
    info: dict[str, Any] = {
        "spans": [],  # [(start, end, clause_id), ...]
        "clauses_total": len(clauses),
        "clauses_sent": 0,
        "chars_total": len(text),
        "chars_sent": 0,
        "body_limited": False,
    }
    if not clauses:
        # 无条款索引：整篇兜底（前预算字符）
        sent_chars = min(len(text), BODY_CHAR_BUDGET)
        info.update(chars_sent=sent_chars, body_limited=len(text) > BODY_CHAR_BUDGET)
        flag = "（正文过长，仅呈现前段）" if info["body_limited"] else ""
        return f"合同正文{flag}：\n{text[:BODY_CHAR_BUDGET]}", info
    if len(text) <= BODY_FULL_LIMIT:
        parts = [
            f"【{c['id']} {str(c.get('heading') or '')[:40]}】\n{text[c['start']:c['end']] or ''}"
            for c in clauses
        ]
        info.update(
            clauses_sent=len(clauses),
            chars_sent=len(text),
            spans=[(c["start"], c["end"], str(c["id"])) for c in clauses],
        )
        return "合同正文（全文，按条款呈现）：\n" + "\n\n".join(parts), info
    parts: list[str] = []
    spans: list[tuple[int, int, str]] = []
    used = 0
    truncated_note = "…（本条款超长，仅呈现前段——引用只能出自已呈现的条款内容）"
    skipped_note = "（正文预算已用尽，后续条款未呈现——不得引用未呈现的条款）"
    for c in clauses:
        block = f"【{c['id']} {str(c.get('heading') or '')[:40]}】\n{text[c['start']:c['end']] or ''}"
        if used + len(block) > BODY_CHAR_BUDGET:
            # Codex P2：超预算条款截断装入（而不是整条丢弃——否则等价写法写在
            # 超长条款里时模型看不到，omission 召回落空）；三轮审计 E：截断条款
            # 的 span 只记**实际发送的前段**，未发送尾部不得成为证据
            remaining = BODY_CHAR_BUDGET - used
            if remaining > MIN_QUOTE_CHARS * 2:
                parts.append(block[:remaining] + truncated_note)
                # span 按精确前缀长度计算（block 前缀=【id + heading截40 + 】\n），
                # 小智娘 P3：-40 估算在长 heading 下会多含未发送字符，不恒 fail-closed
                prefix_len = len(f"【{c['id']} {str(c.get('heading') or '')[:40]}】\n")
                spans.append((
                    c["start"],
                    c["start"] + max(remaining - prefix_len, MIN_QUOTE_CHARS),
                    str(c["id"]),
                ))
                used = BODY_CHAR_BUDGET
            elif skipped_note not in parts:
                parts.append(skipped_note)
            continue
        parts.append(block)
        spans.append((c["start"], c["end"], str(c["id"])))
        used += len(block)
    if not spans:
        # 有条款索引但一条都装不下（全部超预算）：不送正文——空正文+「只能出自
        # 以上条款」是谎话提示，且 allowed 空集会让相关性校验静默失效（门禁 P3-2）
        return "", info
    info.update(
        clauses_sent=len(spans),
        chars_sent=used,
        spans=spans,
        body_limited=True,
    )
    return "合同正文（较长，已按条款节选）：\n" + "\n\n".join(parts), info


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
            f"- 条目 {c['item_id']}（{c['name']}）｜规则结论：{c.get('status') or ('需关注' if c['direction']=='false_positive' else '未找到')}"
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


def _clauses_to_spans(
    clause_ids: set[str], clause_index: Optional[dict]
) -> Optional[list[tuple[int, int, str]]]:
    """条款 id 集合 → 字符区间列表（span 级证据绑定的统一入口）。
    条款无坐标（索引缺失/降级）时返回 None = 退化为存在性校验（fail-open
    有 warning 留痕，与既有降级语义一致）。"""
    spans: list[tuple[int, int, str]] = []
    by_id = {
        str(c.get("id")): c
        for c in ((clause_index or {}).get("clauses") or [])
        if isinstance(c.get("start"), int) and isinstance(c.get("end"), int)
        and c.get("id") and c["start"] < c["end"]
    }
    for cid in clause_ids:
        c = by_id.get(str(cid))
        if c:
            spans.append((c["start"], c["end"], str(c["id"])))
    return spans or None


def _quote_span_hits(
    text: str, quote: str, spans: list[tuple[int, int, str]]
) -> list[str]:
    """返回 quote 在 text 中全部**落在发送区间内**的出现位置所属 clause_id。
    三轮审计 E：证据必须出自模型实际看过的字符区间（截断条款未发送尾部
    不算）；clause_id 由包含位置的 span 派生（不信任模型编号）。
    匹配容差与 quote_supported/locate 一致（Codex P2）：精确匹配之外再用
    clause_index._find_compressed 空白压缩域查找——PDF/Word 换行归一的合法
    引用不得因精确 find 不中被误拒。压缩命中按 quote 长度近似跨度判界
    （原文含空白时跨度略长，判界偏紧=fail-closed 方向）。"""
    starts: list[int] = []
    pos = text.find(quote)
    while pos != -1:
        starts.append(pos)
        pos = text.find(quote, pos + 1)
    compact_q = re.sub(r"\s+", "", quote)
    if compact_q and len(compact_q) >= MIN_QUOTE_CHARS:
        for p in _find_compressed(text, compact_q):
            if p not in starts:
                starts.append(p)
    hits: list[str] = []
    qlen = len(quote)
    for start in starts:
        for (s, e, cid) in spans:
            if s <= start and start + qlen <= e:
                hits.append(cid)
                break
    return hits


def _validate(
    r: dict[str, Any],
    stance: str,
    text: str,
    clause_index: Optional[dict],
    reasoning_clean: str = "",
    allowed_spans: Optional[list[tuple[int, int, str]]] = None,
) -> tuple[bool, Optional[str], Optional[str], Optional[bool]]:
    """五要件硬校验。返回 (accepted, reject_reason, clause_id, clause_ambiguous)。

    reasoning_clean：禁语清洗后的法律逻辑链文本（要件③按它复验长度，
    防「先凑禁语到 30 字、洗完剩空壳」的绕过面）。
    allowed_spans：证据相关性范围（外审 P1-2 + 三轮审计 E）——证据「存在」
    还要「出自本异议允许引用的字符区间」：误报候选=primary 条款区间
    （罪案现场）；漏报候选=实际发送的正文区间。None=不启用（无索引等
    退化场景，存在性校验兜底）。
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
    span_hits: list[str] = []
    if allowed_spans is not None:
        span_hits = _quote_span_hits(text, quote, allowed_spans)
        if not span_hits:
            # 证据真实存在但不在允许引用的区间内：不能拿合同里别处的真话凑要件
            return False, "要件①证据与该异议的条款范围不符", None, None
        clause_ambiguous = len(set(span_hits)) > 1
        clause_id = None if clause_ambiguous else span_hits[0]
    else:
        clause_ambiguous = len(ids) > 1
        clause_id = None if clause_ambiguous else str(ids[0])
    # ② 反证引用或声明无（反证同样受相关性范围约束）
    counter = str(r.get("counter_evidence") or "").strip()
    if not counter:
        return False, "要件②反证缺失", clause_id, clause_ambiguous
    if counter != _DECLARATION and not blind_spot.quote_supported(text, counter):
        return False, "要件②反证原文未能在原文核验", clause_id, clause_ambiguous
    if counter != _DECLARATION and allowed_spans is not None:
        if not _quote_span_hits(text, counter, allowed_spans):
            return False, "要件②反证与该异议的条款范围不符", clause_id, clause_ambiguous
    # ③ 法律逻辑链（按清洗后文本复验）
    reasoning = (reasoning_clean or str(r.get("legal_reasoning") or "").strip())
    if len(reasoning) < MIN_REASONING_CHARS:
        return False, "要件③法律逻辑链过短", clause_id, clause_ambiguous
    # ④ 立场自检（外审批 2 收紧：删 "neutral" 字面量豁免——用户选了具体立场时
    # 模型写 neutral 不再蒙混过关；中立立场由 stance 参数本身为 neutral 表达）
    stance_check = str(r.get("stance_check") or "").strip()
    if stance_check not in {stance, "与立场无关"}:
        return False, f"要件④立场自检不符（{stance_check or '空'}）", clause_id, clause_ambiguous
    return True, None, clause_id, clause_ambiguous


def _eligible_count(items: list[dict[str, Any]]) -> int:
    """符合受理矩阵的候选总数（不截断）——coverage 账目分母。
    走 _eligible_direction 同一谓词（三轮审计 G：单实现防漂移）。"""
    return sum(
        1 for it in items or []
        if isinstance(it, dict) and not it.get("category_na")
        and _eligible_direction(it)
    )


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

    eligible = _eligible_count(items or [])
    candidates = _collect_candidates(items, max_candidates=MAX_CANDIDATES)
    if not candidates:
        # 没有可送审候选（如全 hardline/通过）：合法空结果
        return ObjectionInfo(
            available=True, reason=None, objections=[],
            coverage={
                "eligible": eligible, "sent": 0, "reviewed": 0,
                "candidate_limited": eligible > 0,
                "body_chars_total": len(text), "body_chars_sent": 0,
                "clauses_total": 0, "clauses_sent": 0, "body_limited": False,
            },
        ).model_dump()

    block = _candidates_block(candidates, text, clause_index)
    catalog_lines = [
        f"- {c.get('id')} {str(c.get('heading') or '')[:30]}"
        for c in ((clause_index or {}).get("clauses") or [])
        if c.get("id")
    ]
    catalog = "\n".join(catalog_lines)

    # 外审 P1-1：omission 候选必须有正文可检索（等价写法藏在哪规则不知道，
    # 只给目录=让学生改漏判的卷子却不给卷子）；误报候选证据已有规则摘句，不送正文
    has_omission = any(c["direction"] == "omission" for c in candidates)
    body_block, body_info = "", {"spans": [], "clauses_total": 0, "clauses_sent": 0,
                                 "chars_total": len(text), "chars_sent": 0, "body_limited": False}
    if has_omission:
        body_block, body_info = _body_block(text, clause_index)

    system = objection_prompts.build_system_prompt(stance)
    user = objection_prompts.build_user_prompt(block, catalog, body_block)
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
    accepted_n = 0  # MAX 只数受理条目；每个候选身份最多一条受理（三轮审计 A）
    valid_by_id = {str(it.get("id")): it for it in items or [] if isinstance(it, dict)}
    # 送审候选集合（第二道防线）：候选外的 (item_id, direction) 一律静默丢弃
    # （模型幻觉/注入的未送审组合——连 rejected 都不计：结构性错误非候选）
    sent = {(str(c["item_id"]), str(c["direction"])) for c in candidates}
    # 三轮审计 A：候选身份去重先于配额消费——同一候选的重复申报（换 quote
    # 也一样）不得独占名额。原 ref（quote 前缀）去重被身份去重取代（更强：
    # 同候选换 6 个 quote 也只算 1 条受理）
    served: set[tuple[str, str]] = set()
    reviewed = 0
    for r in parsed or []:
        if not isinstance(r, dict):
            continue
        if accepted_n >= MAX_OBJECTIONS:
            break  # 配额满即停：此后条目未进入任何业务处理，不计 reviewed（审计 P2）
        reviewed += 1
        item_id = str(r.get("item_id") or "")
        direction = str(r.get("direction") or "")
        if (item_id, direction) not in sent:
            continue  # 候选外申报：静默丢弃，不留痕
        it = valid_by_id.get(item_id) or {}
        rc = str(it.get("rule_class") or "heuristic")
        # 方向×类别匹配（第三道）：直接调受理矩阵谓词（小智娘 P3：消灭手写
        # 第二实现——矩阵改动只动 _eligible_direction 一处）
        if _eligible_direction(it) != direction:
            rejected += 1
            continue
        # 禁语清洗先行：要件③对清洗后的文本复验长度（先凑禁语到 30 字再洗空的绕过面）
        reasoning = llm_ask._scrub_banned_echo(scorecard.scrub_forbidden(
            str(r.get("legal_reasoning") or ""))).strip()[:MAX_REASONING_CHARS]
        # 证据相关性范围（span 级，三轮审计 C/E）：误报=primary 条款区间（罪案
        # 现场）；漏报=实际发送的正文区间（截断尾部不算）
        cand = next((c for c in candidates if str(c["item_id"]) == item_id
                     and str(c["direction"]) == direction), None)
        allowed_spans: Optional[list[tuple[int, int, str]]] = None
        if direction == "omission":
            allowed_spans = body_info["spans"] or None
        elif cand and cand.get("scope"):
            allowed_spans = _clauses_to_spans(cand["scope"], clause_index)
            if allowed_spans is None:
                # scope 有值但条款坐标缺失/降级：不得静默跳过相关性校验
                # （肉饼门禁 P2-2：fail-open 开孔）。按规则摘句定位兜底转 span
                fallback = locate_quote_clauses(
                    text, str(it.get("quote") or ""), clause_index or {})
                allowed_spans = _clauses_to_spans({str(i) for i in fallback}, clause_index)
                logger.warning(
                    "Objections: candidate %s scope->span mapping failed, fallback=%s",
                    item_id, bool(allowed_spans))
        elif cand is not None:
            # clause 映射静默降级（scope 为空，门禁 P3-3 fail-open）：按规则摘句
            # 定位兜底转 span，不让相关性校验在被审计的洞上悄悄开孔
            fallback = locate_quote_clauses(
                text, str(it.get("quote") or ""), clause_index or {})
            allowed_spans = _clauses_to_spans({str(i) for i in fallback}, clause_index)
            if allowed_spans is None:
                logger.warning("Objections: false_positive candidate %s has no scope fallback", item_id)
        accepted, why, clause_id, ambiguous = _validate(
            r, stance, text, clause_index, reasoning_clean=reasoning,
            allowed_spans=allowed_spans)
        if accepted:
            if (item_id, direction) in served:
                continue  # 该候选已受理过：重复申报不再入库（先验配额后判重复）
            served.add((item_id, direction))
            accepted_n += 1
        else:
            rejected += 1
        proposal = llm_ask._scrub_banned_echo(scorecard.scrub_forbidden(
            str(r.get("proposal") or ""))).strip()
        # rule_id 服务端唯一决定（外审批 2）：模型可能把提案挂到错误规则上，
        # 输出一律以引擎富化的 items 为准，模型申报的 rule_id 不入库
        rid = it.get("rule_id")
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
        coverage={
            # 候选维（外审批 2）
            "eligible": eligible,
            "sent": len(candidates),
            "reviewed": reviewed,
            "candidate_limited": eligible > len(candidates),
            # 正文维（三轮审计 D：候选没截断 ≠ 全文读完，两维分开记账）
            "body_chars_total": body_info["chars_total"],
            "body_chars_sent": body_info["chars_sent"],
            "clauses_total": body_info["clauses_total"],
            "clauses_sent": body_info["clauses_sent"],
            "body_limited": bool(body_info["body_limited"]),
        },
    ).model_dump()
