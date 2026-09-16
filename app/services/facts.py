"""事实材料（可信度架构 batch3 / A2）：跨条款一致性用的可核对事实。

分段质量/map 路径除「已发现问题」外，还应产出：
- pending_questions：待核实问题
- facts：带证据的核心事实（主体、金额、日期、条件、义务、定义、引用）

预算敏感：条数与字段均封顶，不向一致性轮灌爆 token。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

from app.services import blind_spot, llm_ask, scorecard
from app.services.evidence import build_evidence, document_version_for

logger = logging.getLogger(__name__)

FACT_KINDS = (
    "party",
    "amount",
    "date",
    "condition",
    "obligation",
    "definition",
    "reference",
)

MAX_FACTS = 16
MAX_PENDING = 8
MAX_FACT_VALUE_CHARS = 80
MAX_PENDING_CHARS = 120

# 确定性轻量抽取（不耗 LLM）：金额 / 日期 / 甲乙方，作 map 空观察时的兜底材料
_AMOUNT_RE = re.compile(
    r"(?:人民币|￥|¥|RMB)?\s*"
    r"(?:[0-9]+(?:\.[0-9]+)?|[一二三四五六七八九十百千万亿两壹贰叁肆伍陆柒捌玖拾佰仟]+)"
    r"\s*(?:万元|亿元|元|万)",
)
_DATE_RE = re.compile(
    r"(?:20\d{2}|19\d{2})\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日"
    r"|(?:20\d{2}|19\d{2})[./-]\d{1,2}[./-]\d{1,2}"
)
_PARTY_RE = re.compile(
    r"(?:甲方|乙方|出租方|承租方|买方|卖方|披露方|接收方)"
    r"[（(]?[^）)\n]{0,8}[）)]?[：:]\s*([^\n，。；;]{2,40})"
)


def extract_deterministic_facts(
    text: str,
    *,
    document_version: str = "",
    clause_index: Optional[dict[str, Any]] = None,
    budget: int = 8,
) -> list[dict[str, Any]]:
    """无 LLM 的轻量事实抽取：保证一致性轮至少有材料，即使 map 观察为空。"""
    if not (text or "").strip():
        return []
    doc_ver = document_version or document_version_for(text)
    out: list[dict[str, Any]] = []
    seen: set[str] = set()

    def _add(kind: str, label: str, value: str, quote: str) -> None:
        if len(out) >= budget:
            return
        key = f"{kind}|{value}"
        if key in seen:
            return
        if not quote or not blind_spot.quote_supported(text, quote):
            # 退一步：用 value 本身当 quote
            if value and blind_spot.quote_supported(text, value):
                quote = value
            else:
                return
        seen.add(key)
        evidence = build_evidence(
            text=text,
            quote=quote[: blind_spot.MAX_QUOTE_CHARS],
            parse_source="fact",
            document_version=doc_ver,
            clause_index=clause_index,
        )
        out.append(
            {
                "kind": kind,
                "label": label,
                "value": value[:MAX_FACT_VALUE_CHARS],
                "evidence": evidence,
            }
        )

    for m in _PARTY_RE.finditer(text):
        name = (m.group(1) or "").strip()
        if name:
            _add("party", "主体", name, m.group(0).strip())
    for m in _AMOUNT_RE.finditer(text):
        _add("amount", "金额", m.group(0).strip(), m.group(0).strip())
    for m in _DATE_RE.finditer(text):
        _add("date", "日期", m.group(0).strip(), m.group(0).strip())
    return out


def parse_map_extras(raw: str) -> tuple[list[dict[str, Any]], list[str]]:
    """从质量 map JSON 中解析 facts / pending_questions（容忍缺省）。"""
    if not raw:
        return [], []
    text = raw.strip()
    fence = re.match(r"^```(?:json)?\s*([\s\S]*?)\s*```$", text)
    if fence:
        text = fence.group(1).strip()
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{[\s\S]*\}", text)
        if not m:
            return [], []
        try:
            obj = json.loads(m.group(0))
        except json.JSONDecodeError:
            return [], []
    if not isinstance(obj, dict):
        return [], []
    facts_raw = obj.get("facts")
    if not isinstance(facts_raw, list):
        facts_raw = []
    pending_raw = obj.get("pending_questions")
    if not isinstance(pending_raw, list):
        pending_raw = []
    facts: list[dict[str, Any]] = []
    for row in facts_raw:
        if isinstance(row, dict):
            facts.append(row)
    pending: list[str] = []
    for p in pending_raw:
        if isinstance(p, str) and p.strip():
            pending.append(p.strip()[:MAX_PENDING_CHARS])
        elif isinstance(p, dict):
            q = str(p.get("question") or p.get("text") or "").strip()
            if q:
                pending.append(q[:MAX_PENDING_CHARS])
    return facts, pending[:MAX_PENDING]


def clean_facts(
    raw_facts: list[dict[str, Any]],
    text: str,
    *,
    document_version: str = "",
    clause_index: Optional[dict[str, Any]] = None,
) -> list[dict[str, Any]]:
    """清洗模型事实：白名单 kind、quote 核验、挂 evidence。"""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    doc_ver = document_version or document_version_for(text)
    for row in raw_facts:
        if len(out) >= MAX_FACTS:
            break
        kind = str(row.get("kind") or "").strip().lower()
        if kind not in FACT_KINDS:
            continue
        value = str(row.get("value") or "").strip()[:MAX_FACT_VALUE_CHARS]
        label = str(row.get("label") or _default_label(kind)).strip()[:40]
        quote = str(row.get("quote") or "").strip().strip("「」\"'“”")
        if not value:
            continue
        value = llm_ask._scrub_banned_echo(scorecard.scrub_forbidden(value)).strip()
        label = llm_ask._scrub_banned_echo(scorecard.scrub_forbidden(label)).strip()
        if not value or "【已过滤】" in value:
            continue
        if quote and not blind_spot.quote_supported(text, quote):
            quote = ""
        if not quote and blind_spot.quote_supported(text, value):
            quote = value
        if not quote:
            continue
        key = f"{kind}|{value}"
        if key in seen:
            continue
        seen.add(key)
        evidence = build_evidence(
            text=text,
            quote=quote[: blind_spot.MAX_QUOTE_CHARS],
            parse_source="fact",
            document_version=doc_ver,
            clause_index=clause_index,
        )
        out.append(
            {
                "kind": kind,
                "label": label or _default_label(kind),
                "value": value,
                "evidence": evidence,
            }
        )
    return out


def merge_facts(*groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for group in groups:
        for f in group or []:
            key = f"{f.get('kind')}|{f.get('value')}"
            if key in seen:
                continue
            seen.add(key)
            out.append(f)
            if len(out) >= MAX_FACTS:
                return out
    return out


def facts_material_lines(facts: list[dict[str, Any]], limit: int = 12) -> str:
    """一致性轮素材：键值 + 原文摘句，预算内。"""
    lines: list[str] = []
    for f in (facts or [])[:limit]:
        quote = ""
        ev = f.get("evidence") or {}
        if isinstance(ev, dict):
            quote = str(ev.get("quote") or "")
        lines.append(
            f"- [事实/{f.get('kind')}] {f.get('label')}:{f.get('value')}｜原文：{quote}"
        )
    return "\n".join(lines)


def _default_label(kind: str) -> str:
    return {
        "party": "主体",
        "amount": "金额",
        "date": "日期",
        "condition": "条件",
        "obligation": "义务",
        "definition": "定义",
        "reference": "引用",
    }.get(kind, "事实")
