# -*- coding: utf-8 -*-
"""B7-5a「AI 发现」流水线合并服务（spec-b75a-ai-findings.md）。

合并「AI 观察」（quality）与「需你确认」（verify 疑点）为统一发现清单：
- finding_key 结构化防撞号（codex #173 二轮：同标题「待核实」+空引句
  两条不同问题必须不同号——键纳入 source+问题正文+证据身份）
- 观察↔其派生问题合并为一条发现（共用核对证据/确认状态/物化检查项）
- 跨源标注：与规则需关注条目同主题默认标「关联规则：{条目名}」
  （同主题≠结论一致，不自动标「互相印证」）
- 确认（事实类）→ 物化为 AI 发现检查项（tag_source="ai_finding"），
  复用三卡/追问/报告明细全部既有机制；争议（反悔）→ 检查项停用
  （状态改「已争议撤回」，生成/追问/牵连入口资格自然失效）；再确认
  → 复活（状态机闭环）

纯读/纯状态表操作，零 LLM 调用。
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import tempfile
import time
from typing import Any, Optional

_STATUS_DDL = (
    "CREATE TABLE IF NOT EXISTS ai_finding_status ("
    "review_id TEXT NOT NULL, finding_key TEXT NOT NULL,"
    "status TEXT NOT NULL, updated_at REAL NOT NULL,"
    "PRIMARY KEY (review_id, finding_key))"
)

_VALID_STATUS = ("viewed", "confirmed", "disputed")

_SEP = "\x1f"  # 键内分隔符：防字段拼接歧义（codex #173 二轮）


def _sha(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:16]


def _obs_key(dim: str, title: str, clause_id: str, quote: str) -> str:
    """观察键：维度+标题+条款+引句 全部纳入（同标题不同条款=不同号）。"""
    return f"obs:{dim}:{_sha(_SEP.join((dim, title, clause_id or '', quote or '')))[:16]}"


def _ver_key(q: dict[str, Any]) -> str:
    """疑点键：有 claim_id 用之；无则 来源+完整问题正文+证据身份。
    现状代码会把多个不同问题统一命名「待核实」且引句为空——仅
    title+quote 必撞号（codex 二轮实锤），故纳入更多字段。"""
    claim = str(q.get("claim_id") or "")
    if claim:
        return f"ver:{claim}"
    src = str(q.get("source") or "")
    qtext = str(q.get("question") or "")
    clause_id = str(q.get("clause_id") or "")
    quote = str(q.get("quote") or "")
    return f"ver:{_sha(_SEP.join((src, qtext, clause_id, quote)))[:16]}"


def _db_path() -> str:
    """与审查单库同文件（impact.py/revision.py 同款 env 约定）。"""
    env = (os.getenv("STORE_DB_PATH") or "").strip()
    if env:
        return env
    return os.path.join(tempfile.gettempdir(), "agent-t-reviews.db")


def _q_db() -> sqlite3.Connection:
    """打开状态表连接（懒建表，随库走）。"""
    conn = sqlite3.connect(_db_path())
    conn.execute(_STATUS_DDL)
    return conn


def _statuses(review_id: str) -> dict[str, str]:
    conn = _q_db()
    try:
        rows = conn.execute(
            "SELECT finding_key, status FROM ai_finding_status"
            " WHERE review_id=?", (review_id,)).fetchall()
        return {r[0]: r[1] for r in rows}
    finally:
        conn.close()


def _save_status(review_id: str, key: str, status: str) -> None:
    conn = _q_db()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO ai_finding_status"
            " (review_id, finding_key, status, updated_at) VALUES (?,?,?,?)",
            (review_id, key, status, time.time()))
        conn.commit()
    finally:
        conn.close()


def _rule_labels(row: dict[str, Any]) -> list[str]:
    """规则需关注条目名（跨源关联标注候选）。"""
    return [str(i.get("name") or "") for i in (row.get("items") or [])
            if isinstance(i, dict) and i.get("status") == "需关注" and i.get("name")]


def _rule_link(title: str, comment: str, rule_names: list[str]) -> str:
    """同主题检测（简单包含口径）：规则名出现在发现标题/说明中 →
    「关联规则：{名}」；未命中不标注（同主题≠结论一致，不自动升级
    「互相印证」——codex 二轮）。"""
    text = title + _SEP + comment
    for name in rule_names:
        if name and name in text:
            return f"关联规则：{name}"
    return ""


def _human_state(
    q: Optional[dict[str, Any]], key: str,
    statuses: dict[str, str], default_state: str,
) -> tuple[str, Optional[str]]:
    """人的确认决定 与 核对进度 分离（codex 四轮 P1-1）。

    rechecked 只是核对进度（再核完成），不是人的新决定——决定优先级：
    ① question.decision.choice（决定记录权威）② question.status 直陈
    ③ rechecked → 状态表（统一入口每次拍板都落表）④ 默认态。"""
    if not isinstance(q, dict):
        return statuses.get(key) or default_state, None
    check = str(q.get("status") or "")
    d = q.get("decision")
    choice = str(d.get("choice") or "") if isinstance(d, dict) else ""
    if choice == "confirm":
        return "confirmed", check
    if choice == "dispute":
        return "disputed", check
    if check in ("confirmed", "disputed"):
        return check, check
    if check == "rechecked":
        return statuses.get(key) or "confirmed", check
    return statuses.get(key) or default_state, check


def review_findings(row: dict[str, Any]) -> dict[str, Any]:
    """聚合统一发现清单（纯读）。row=审查单原始数据（含 quality/verify）。

    返回 {"available", "findings": [{finding_key, source_type(observ|
    question|standalone), fact_type, dimension, title, comment, quote,
    clause_id, evidence, status, rule_link, materialized, item_id}]}"""
    review_id = str(row.get("id") or "")
    statuses = _statuses(review_id)
    rule_names = _rule_labels(row)
    observations = [(row.get("quality") or {}).get("observations") or []]
    obs_list = observations[0]
    questions = (row.get("verify") or {}).get("questions") or []

    items = row.get("items") or []
    item_by_finding: dict[str, dict[str, Any]] = {}
    for it in items:
        if isinstance(it, dict) and it.get("tag_source") == "ai_finding" \
                and it.get("finding_key"):
            item_by_finding[it["finding_key"]] = it

    findings: list[dict[str, Any]] = []
    used_question_ids: set[str] = set()

    def _qid(q: dict[str, Any]) -> str:
        return str(q.get("id") or "")

    # 派生配对用稳定身份（codex 三轮 P1-3）：核验问题从观察派生时固定带
    # source="quality_obs" + source_ref="obs:{下标}"（verify.py _add）——
    # 按下标配对，绝不按 title 配（同标题不同条款会配错对、串确认状态）
    def _obs_idx(q: dict[str, Any]) -> Optional[int]:
        if str(q.get("source") or "") != "quality_obs":
            return None
        ref = str(q.get("source_ref") or "")
        tail = ref[4:] if ref.startswith("obs:") else ""
        return int(tail) if tail.isdigit() else None

    derived_by_obs: dict[int, dict[str, Any]] = {}
    for q in questions:
        if isinstance(q, dict):
            idx = _obs_idx(q)
            if idx is not None:
                derived_by_obs[idx] = q

    # 观察类发现（与其派生问题按下标配对合并——codex 三轮 P1-3）
    for obs_i, obs in enumerate(obs_list):
        if not isinstance(obs, dict):
            continue
        dim = str(obs.get("dimension") or "")
        title = str(obs.get("title") or "").strip()
        comment = str(obs.get("comment") or "").strip()
        quote = str(obs.get("quote") or "").strip()
        clause_id = str(obs.get("clause_id") or "")
        key = _obs_key(dim, title, clause_id, quote)
        derived = derived_by_obs.get(obs_i)
        fact_type = dim != "impact"
        default_state = "pending" if fact_type else "viewed"
        obs_ev = obs.get("evidence")
        ev_obj = obs_ev if isinstance(obs_ev, dict) else None
        human, check = _human_state(derived, key, statuses, default_state)
        entry: dict[str, Any] = {
            "finding_key": key,
            "source_type": "observ",
            "fact_type": fact_type,
            "dimension": dim,
            "title": title,
            "comment": comment,
            "quote": quote,
            "clause_id": clause_id,
            # 证据对象（随行物化，codex 四轮 P2-2）与核对状态串分开：
            # verification=核对结论字符串；evidence=EvidenceRef 对象
            "verification": None,
            "evidence": ev_obj,
            "status": human,
            "check_state": check,
            "question_id": None,
            "rule_link": _rule_link(title, comment, rule_names),
            "materialized": key in item_by_finding,
            "item_id": (item_by_finding.get(key) or {}).get("id"),
            "item_status": (item_by_finding.get(key) or {}).get("status"),
        }
        if derived is not None and isinstance(derived, dict):
            used_question_ids.add(_qid(derived))
            entry["source_type"] = "observ+question"
            entry["question_id"] = derived.get("id")
            entry["verification"] = str(derived.get("verification") or "") or None
            if ev_obj is None and isinstance(derived.get("evidence"), dict):
                entry["evidence"] = derived.get("evidence")
        findings.append(entry)

    # 独立疑点（pack_verify 源，非观察派生）
    for q in questions:
        if not isinstance(q, dict) or _qid(q) in used_question_ids:
            continue
        key = _ver_key(q)
        q_ev = q.get("evidence")
        human, check = _human_state(q, key, statuses, "pending")
        findings.append({
            "finding_key": key,
            "source_type": "standalone",
            "fact_type": True,  # 「请确认是否属实」= 事实类
            "dimension": "",
            "title": str(q.get("title") or "").strip(),
            "comment": str(q.get("question") or "").strip(),
            "quote": str(q.get("quote") or "").strip(),
            "clause_id": str(q.get("clause_id") or ""),
            "verification": str(q.get("verification") or "") or None,
            "evidence": q_ev if isinstance(q_ev, dict) else None,
            "status": human,
            "check_state": check,
            "question_id": q.get("id"),
            "rule_link": _rule_link(str(q.get("title") or ""),
                                    str(q.get("question") or ""), rule_names),
            "materialized": key in item_by_finding,
            "item_id": (item_by_finding.get(key) or {}).get("id"),
            "item_status": (item_by_finding.get(key) or {}).get("status"),
        })

    return {"available": True, "findings": findings}


def materialize(
    *, finding_key: str, quote: str, clause_id: str,
    note: str, title: str, evidence: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """构造 AI 发现检查项（不落库，调用方经 store.update 追加）。

    evidence（codex 四轮 P2-2）：原发现的有效证据对象随行入项——读路径
    的主张标注/证据链按 item.evidence 派生，不随行则报告「无证据票据」、
    主张编号为空。"""
    item_id = "ai_" + _sha(finding_key)[:12]
    return {
        "id": item_id,
        "name": f"【AI 发现】{title}",
        "status": "需关注",
        "note": note,
        "quote": quote,
        "hits": [],
        "evidence": evidence if isinstance(evidence, dict) else None,
        "evidence_start": None,
        "evidence_end": None,
        "primary_clause_id": clause_id or None,
        "clause_ids": [clause_id] if clause_id else [],
        "tag_source": "ai_finding",
        "finding_key": finding_key,
    }


def set_status(store: Any, review_id: str, *, finding_key: str,
               status: str) -> dict[str, Any]:
    """状态入口（物化/停用/复活/留痕）。

    注意：带 question_id 的派生发现，其确认走 confirm 路由链
    （compose_decision 决定记录+Design B 校验，本函数不碰 verify），
    本函数只负责状态表与检查项物化——调用方先走确认链再调本函数。"""
    if status not in _VALID_STATUS:
        return {"status": "error", "error": f"非法状态 {status!r}"}
    row = store.get(review_id)
    if row is None:
        return {"status": "error", "error": "review_not_found"}
    # finding_key 口径与页面同源：统一在 derive_claims_view 归一化视图上解析
    # （claim_id 是读路径派生属性——原行没有、归一后有；不归一则 GET 下发的
    # ver:{claim_id} 键在这里会解析不到，codex 复批预堵）
    from app.services.evidence import derive_claims_view
    norm_row, _w, _cw = derive_claims_view(row)
    finding = next((f for f in review_findings(norm_row).get("findings") or []
                    if f["finding_key"] == finding_key), None)
    if finding is None:
        return {"status": "error", "error": "finding_not_found"}

    # spec §四.2：无原文引句且无坐标的事实类不可物化（清单条目必须有
    # 原文依据）——如实拒绝；观点类本就不物化，无此限制
    if status == "confirmed" and finding["fact_type"] \
            and not (finding.get("quote") or finding.get("clause_id")):
        return {"status": "error",
                "error": "该发现无原文引句与坐标，不能作为清单条目（无原文依据）"}
    # 统一落表（codex 四轮 P2-3）：观点类的已阅/争议同样持久化——此前只在
    # 事实类分支保存，观点争议刷新即回「已阅」。观点仍不可物化（下方守卫）
    _save_status(review_id, finding_key, status)

    items = list(row.get("items") or [])
    existing = next((i for i in items if isinstance(i, dict)
                     and i.get("finding_key") == finding_key
                     and i.get("tag_source") == "ai_finding"), None)
    item_id = (existing or {}).get("id")

    if status == "confirmed" and finding["fact_type"]:
        if existing is None:
            new_item = materialize(
                finding_key=finding_key,
                quote=finding.get("quote") or "",
                evidence=finding.get("evidence"),
                clause_id=finding.get("clause_id") or "",
                note=finding.get("comment") or "",
                title=finding.get("title") or "")
            items.append(new_item)
            item_id = new_item["id"]
        else:
            # 复活（争议后再确认）
            existing["status"] = "需关注"
        store.update(review_id, items=items)
    elif status == "disputed" and existing is not None:
        # 反悔：物化项停用（资格判定走 status，历史留痕保留）
        existing["status"] = "已争议撤回"
        store.update(review_id, items=items)

    return {"status": "ok", "finding_status": status,
            "item_id": item_id, "materialized": item_id is not None}
