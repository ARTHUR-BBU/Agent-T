# -*- coding: utf-8 -*-
"""M7 连带影响服务（B7-2b，spec-b7-2-impact.md §三）。

- 选定草案（selected_drafts）：与生成/重审三分——生成≠选定≠解决；
  选定时校验草案归属（当前审查单+当前检查项，spec §3.1 / codex 复批）。
- 连带影响：条款簇定位（词族 YAML 配置化，按 item id，检索检查条款正文）
  → 命中则调 impact 逐摘录分析一致性；**查不到相关簇 → 如实标「未覆盖」
  （不调 LLM）**；「未覆盖」是定位结果，绝不表述为「没有冲突」。
- 连带影响只分析**用户选中的那一版**草案（按 draft_id 取，未选定/选别版拒绝）。
- 报告尾部固定重审提示（RE_REVIEW_NOTICE，选定草案≠解决问题）。
- 失败语义沿用 #148：invalid_json/incomplete=业务降级，llm_error/no_llm_key
  =基础设施降级；预算拒绝走 emit_skip 显式落账。
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import time
import uuid
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Optional

import yaml

from app.prompts import impact_prompts
from app.services import evidence as evidence_module
from app.services import llm_ask, llm_call_log, llm_client, store as store_module
from app.services.revision import (
    _connect,
    _Degraded,
    consume_budget,
    impact_limit_from_env,
    remaining_budget,
)

logger = logging.getLogger(__name__)

_KEYWORDS_PATH = Path(__file__).resolve().parents[2] / "config" / "impact_keywords.yaml"


@lru_cache(maxsize=1)
def _load_families() -> dict[str, list[str]]:
    """词族配置（YAML 可审）；结构非法 fail-closed 抛错（对齐清单配置纪律）。"""
    with _KEYWORDS_PATH.open("r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    families = cfg.get("families")
    if not isinstance(families, dict) or not families:
        raise ValueError("impact_keywords.yaml 缺 families 或为空（fail-closed）")
    for item_id, words in families.items():
        if (not isinstance(words, list) or not words
                or not all(isinstance(w, str) and w.strip() for w in words)):
            raise ValueError(
                f"impact_keywords.yaml 词族 {item_id!r} 非法（须为非空字符串列表）")
    return {str(k): [str(w) for w in v] for k, v in families.items()}


# 摘录窗口：命中关键词前后各 40 字符（确保摘录里带条款正文而非孤立词）
_EXCERPT_WINDOW = 40


def find_related_clauses(
    text: str,
    item_id: str,
    exclude_positions: Optional[list[tuple[int, int]]] = None,
    clause_index: Optional[dict[str, Any]] = None,
    exclude_clause_ids: Optional[set[str]] = None,
) -> list[dict[str, Any]]:
    """按词族在合同全文检索**其他条款**的摘录（关键词+位置+条款窗原文）。

    排除被改条款自身（codex #154 复批 3）：命中点落在被改条款的证据坐标
    区间内、或按条款索引定位到被改条款的 clause_id，则不计入——否则会把
    原条款自己当成「其他相关条款」误报已覆盖。检索范围=词族命中处
    （不做全卷扫描承诺，spec §3.2）；命中即带原文与坐标。"""
    families = _load_families()
    keywords = families.get(item_id) or []
    ranges = exclude_positions or []
    exclude_ids = exclude_clause_ids or set()
    excerpts: list[dict[str, Any]] = []
    seen: set[int] = set()
    for kw in keywords:
        start = 0
        while True:
            pos = text.find(kw, start)
            if pos < 0:
                break
            start = pos + len(kw)
            # 排除原条款自身：证据坐标区间命中 or 条款索引归属命中
            if any(lo <= pos < hi for lo, hi in ranges):
                continue
            if exclude_ids and clause_index:
                cid = evidence_module._clause_at(clause_index, pos)
                if cid and cid in exclude_ids:
                    continue
            # 相邻命中去重（同一窗口内多个词命中合并为一条摘录）
            bucket = pos // _EXCERPT_WINDOW
            if bucket in seen:
                continue
            seen.add(bucket)
            lo = max(0, pos - _EXCERPT_WINDOW)
            hi = min(len(text), pos + len(kw) + _EXCERPT_WINDOW)
            # 摘录窗在排除区间边界裁剪：不把被改条款原文渗进「其他条款」
            # 摘录（否则摘录挂着原条款尾巴，定位语义被污染）
            for rlo, rhi in ranges:
                if rlo < hi and rhi > lo:  # 窗口与排除区间相交
                    if pos >= rhi:
                        lo = max(lo, rhi)
                    elif pos < rlo:
                        hi = min(hi, rlo)
            excerpt = text[lo:hi].replace("\n", " ").strip()
            if excerpt:
                excerpts.append({
                    "index": len(excerpts),
                    "keyword": kw,
                    "position": pos,
                    "excerpt": excerpt,
                })
    return excerpts


_SELECTED_DDL = (
    "CREATE TABLE IF NOT EXISTS selected_drafts ("
    "review_id TEXT NOT NULL, item_id TEXT NOT NULL,"
    "draft_id TEXT NOT NULL, stance TEXT NOT NULL,"
    "selected_at REAL NOT NULL,"
    "PRIMARY KEY (review_id, item_id))")


def _draft_stances(payload: dict[str, Any]) -> list[str]:
    """从草案 payload 提取合法版别列表（稳妥/平衡/进取）。"""
    stances = []
    for v in payload.get("versions") or []:
        if isinstance(v, dict) and isinstance(v.get("stance"), str) \
                and v["stance"].strip():
            stances.append(v["stance"].strip())
    return stances


def select_draft(store: Any, review_id: str, item_id: str,
                 draft_id: str, stance: str) -> dict[str, Any]:
    """用户选定草案：校验归属（当前审查单+当前检查项）与**版别**后落库。

    选定≠生成≠重审（三分，spec §3.1）；一个 draft_id 装三版（稳妥/平衡/
    进取），选定必须落到具体版别（codex #154 复批 2）——否则连带影响
    无法知道分析的是哪一版。跨审查单/跨检查项/版别不符一律拒绝。"""
    review = store.get(review_id)
    if review is None:
        return {"status": "not_found", "reason": "review_not_found"}
    if not any(i.get("id") == item_id for i in review.get("items") or []):
        return {"status": "not_found", "reason": "item_not_found"}
    with _connect() as conn:
        row = conn.execute(
            "SELECT review_id, item_id, status, payload FROM revision_drafts"
            " WHERE id = ?", (draft_id,)).fetchone()
    if row is None or row[0] != review_id or row[1] != item_id:
        return {"status": "not_found", "reason": "draft_not_found"}
    if row[2] != "ok":
        # 降级草稿没有可分析对象，不可选定
        return {"status": "not_supported", "reason": "draft_degraded"}
    if not isinstance(stance, str) or stance.strip() not in \
            _draft_stances(json.loads(row[3])):
        return {"status": "not_supported", "reason": "stance_invalid",
                "valid_stances": _draft_stances(json.loads(row[3]))}
    with _connect() as conn:
        conn.execute(_SELECTED_DDL)
        conn.execute(
            "INSERT OR REPLACE INTO selected_drafts"
            " (review_id, item_id, draft_id, stance, selected_at)"
            " VALUES (?,?,?,?,?)",
            (review_id, item_id, draft_id, stance.strip(), time.time()))
    return {"status": "selected", "draft_id": draft_id, "stance": stance.strip()}


def get_selected(review_id: str, item_id: str) -> Optional[tuple[str, str]]:
    """返回 (draft_id, stance)；未选定为 None。"""
    with _connect() as conn:
        conn.execute(_SELECTED_DDL)
        row = conn.execute(
            "SELECT draft_id, stance FROM selected_drafts"
            " WHERE review_id=? AND item_id=?",
            (review_id, item_id)).fetchone()
    return (row[0], row[1]) if row else None


_IMPACT_REPORTS_DDL = (
    "CREATE TABLE IF NOT EXISTS impact_reports ("
    "id TEXT PRIMARY KEY, review_id TEXT NOT NULL, item_id TEXT NOT NULL,"
    "draft_id TEXT NOT NULL, created_at REAL NOT NULL, trace_id TEXT,"
    "payload TEXT NOT NULL, status TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '')")


def _save_impact_report(review_id: str, item_id: str, draft_id: str,
                        trace_id: str, payload: dict[str, Any],
                        status: str, reason: str = "") -> None:
    with _connect() as conn:
        conn.execute(_IMPACT_REPORTS_DDL)
        conn.execute(
            "INSERT INTO impact_reports (id, review_id, item_id, draft_id,"
            " created_at, trace_id, payload, status, reason)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (uuid.uuid4().hex[:16], review_id, item_id, draft_id, time.time(),
             trace_id, json.dumps(payload, ensure_ascii=False), status, reason))


def _purge_hook(conn: sqlite3.Connection, expired_ids: list[str]) -> None:
    """store 过期删除钩子（同连接同事务，对齐 revision._purge_hook）——
    审查单删则选定记录/影响报告一并删。"""
    marks = ",".join("?" for _ in expired_ids)
    conn.execute(
        f"DELETE FROM selected_drafts WHERE review_id IN ({marks})", expired_ids)
    conn.execute(
        f"DELETE FROM impact_reports WHERE review_id IN ({marks})", expired_ids)


# import 时挂载（routes_revision → revision → impact 链保证应用启动即注册）
store_module.PURGE_HOOKS.append(_purge_hook)


def _parse_findings(raw: str, n_excerpts: int) -> list[dict[str, Any]]:
    """严格 Schema（对齐 revision._parse_versions 纪律）：
    findings 字段**必须显式在场且为列表**（codex #154 复批 4：缺字段/空对象
    =模型未完成分析，一律降级，不得把「没给」当「没有」）；每项 index 在
    摘录范围内且 note 为非空字符串。仅显式 [] = 合法的「无不一致」。"""
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text)
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise _Degraded("invalid_json") from exc
    if not isinstance(data, dict):
        raise _Degraded("incomplete")
    findings = data.get("findings")
    if not isinstance(findings, list):
        # 缺字段/{}/null/非列表 → 模型未按约定完成输出，整项降级（非空结果）
        raise _Degraded("incomplete")
    cleaned: list[dict[str, Any]] = []
    for f in findings:
        if not isinstance(f, dict):
            raise _Degraded("incomplete")
        idx, note = f.get("index"), f.get("note")
        if not isinstance(idx, int) or isinstance(idx, bool) \
                or not (0 <= idx < n_excerpts):
            raise _Degraded("incomplete")
        if not isinstance(note, str) or not note.strip():
            raise _Degraded("incomplete")
        cleaned.append({"index": idx, "note": note.strip()})
    return cleaned


def _default_llm_call(api_key: str, system: str, user: str) -> str:
    """生产通道：DeepSeek（OpenAI 兼容），node=impact（阀门 8000，#137）。"""
    llm_timeout = float(os.getenv("LLM_TIMEOUT_SECONDS", "180"))
    return llm_client.chat_completion(
        api_key=api_key,
        url=llm_ask._deepseek_chat_url(),
        model=llm_ask._model_for("DEEPSEEK_MODEL", llm_ask.DEFAULT_DEEPSEEK_MODEL,
                                 purpose="impact"),
        system=system,
        user=user,
        temperature=0.3,
        timeout=llm_timeout,
    )


def _load_draft_text(draft_id: str, stance: str) -> Optional[str]:
    """按 draft_id+**选中版别** 取草案文本（codex #154 复批 2：
    一个 draft_id 装三版，分析对象=选定版别的那一版，绝非固定第一版）。"""
    with _connect() as conn:
        row = conn.execute(
            "SELECT payload, status FROM revision_drafts WHERE id=?",
            (draft_id,)).fetchone()
    if row is None or row[1] != "ok":
        return None
    payload = json.loads(row[0])
    for v in payload.get("versions") or []:
        if isinstance(v, dict) and v.get("stance") == stance:
            return str(v.get("text") or "")
    return None


def _clause_span(clause_index: dict[str, Any], cid: str) -> Optional[tuple[int, int]]:
    """按条款 id 查完整坐标（右开区间 [start, end)，与索引约定一致）。"""
    for c in clause_index.get("clauses") or []:
        if str(c.get("id") or "") == cid:
            s, e = c.get("start"), c.get("end")
            if isinstance(s, int) and isinstance(e, int) and e >= s:
                return (s, e)
    return None


def _exclude_ranges_for_item(item: dict[str, Any],
                             clause_index: Optional[dict[str, Any]]
                             ) -> tuple[list[tuple[int, int]], set[str]]:
    """汇总被改条款的排除依据（codex #154 复批 3 + P2 追加）：

    - 证据引句坐标区间（引句可能只覆盖条款的一部分）；
    - **被排除条款的完整 span**（按条款索引查 start/end）——引句之后的
      条款尾巴同样不得渗进「其他条款」摘录；
    - 条款 id 集合（primary_clause_id + clause_ids + 证据起点定位）。
    注意：索引不可用/查不到 span 时本函数只回引句区间——此时调用方
    （analyze_impact）必须先过 reliable 可靠性门禁，不达标直接保守
    受限，不放行正常分析（codex P2 复批）。"""
    ranges: list[tuple[int, int]] = []
    ids: set[str] = set()
    s, e = item.get("evidence_start"), item.get("evidence_end")
    if isinstance(s, int) and isinstance(e, int) and e >= s:
        ranges.append((s, e + 1))
    if item.get("primary_clause_id"):
        ids.add(str(item["primary_clause_id"]))
    for cid in item.get("clause_ids") or []:
        if cid:
            ids.add(str(cid))
    if isinstance(s, int) and clause_index:
        cid = evidence_module._clause_at(clause_index, s)
        if cid:
            ids.add(cid)
    if clause_index:
        for cid in ids:
            span = _clause_span(clause_index, cid)
            if span is not None:
                ranges.append(span)
    return ranges, ids


def analyze_impact(
    store: Any,
    review_id: str,
    item_id: str,
    draft_id: str,
    llm_call: Optional[Callable[[str, str, str], str]] = None,
) -> dict[str, Any]:
    """连带影响分析：只分析用户选中的那一版草案（spec §3.1）。

    返回 {"status": "ok"|"uncovered"|"degraded"|"budget_exceeded"|
    "not_found"|"not_supported", ...}；预算在定位之后、LLM 之前扣减
    （未覆盖/未选定不消耗额度）。"""
    review = store.get(review_id)
    if review is None:
        return {"status": "not_found", "reason": "review_not_found"}
    item = next((i for i in review.get("items") or [] if i.get("id") == item_id),
                None)
    if item is None:
        return {"status": "not_found", "reason": "item_not_found"}

    # codex #154 复批 2：选定落到具体版别，分析对象=选中的 (draft, stance)
    selected = get_selected(review_id, item_id)
    if selected is None:
        return {"status": "not_supported", "reason": "draft_not_selected",
                "message": "请先选定草案（含版别），再发起连带影响分析"}
    selected_draft, selected_stance = selected
    if selected_draft != draft_id:
        # 连带影响始终分析「用户选中的那一版」——传了别版=拒绝（不迁就）
        return {"status": "not_supported", "reason": "draft_mismatch",
                "message": "连带影响只分析已选定的草案版本"}

    draft_text = _load_draft_text(draft_id, selected_stance)
    if draft_text is None:
        return {"status": "not_found", "reason": "draft_not_found"}

    # 条款簇定位（本地检索，零费用）：查不到相关簇 → 如实标「未覆盖」，
    # 不调 LLM、不装覆盖、不消耗额度（spec §3.2 措辞纪律）。
    # codex #154 复批 1：真实审查单正文存 text 字段（parsed_text 是管道
    # 入参不是落库字段——按真实形态读，否则永远假「未覆盖」）
    contract_text = str(review.get("text") or "")
    clause_index = review.get("clause_index") \
        if isinstance(review.get("clause_index"), dict) else None
    if contract_text:
        excl_ranges, excl_ids = _exclude_ranges_for_item(item, clause_index)
        # 复批 P2 收紧（codex 2026-10-29）：无法可靠取得被改条款完整范围
        # （缺条款索引/定位不到条款）时，仅按引句裁剪挡不住尾巴渗漏与
        # 标题误捕——保守受限：停止正常关联分析，不调 LLM、不扣额度、
        # 不报覆盖。真实 done 审查单均带条款索引，此路径属异常数据形态。
        reliable = bool(clause_index) and bool(excl_ids) and any(
            _clause_span(clause_index, cid) is not None for cid in excl_ids)
        if not reliable:
            report = {
                "covered": False,
                "uncovered_reason": (
                    "无法可靠定位被修改条款的完整范围（缺条款索引或无法"
                    "定位条款边界），为避免把原条款误当关联条款，本次不"
                    "进行关联分析（不代表没有冲突）"),
                "notice": impact_prompts.RE_REVIEW_NOTICE,
                "findings": [],
            }
            _save_impact_report(review_id, item_id, draft_id, "", report,
                                "uncovered")
            return {"status": "uncovered", "report": report}
        excerpts = find_related_clauses(contract_text, item_id,
                                        exclude_positions=excl_ranges,
                                        clause_index=clause_index,
                                        exclude_clause_ids=excl_ids)
    else:
        excerpts = []
    if not excerpts:
        report = {
            "covered": False,
            "uncovered_reason": (
                "未在合同全文检索到该条款之外可能与草案联动的其他条款"
                "（不代表没有冲突）"
                if contract_text else
                "审查单缺少合同全文，无法定位相关条款簇（不代表没有冲突）"),
            "notice": impact_prompts.RE_REVIEW_NOTICE,
            "findings": [],
        }
        _save_impact_report(review_id, item_id, draft_id, "", report,
                            "uncovered")
        return {"status": "uncovered", "report": report}

    limit = impact_limit_from_env()
    if not consume_budget(review_id, "impact", limit):
        llm_call_log.emit_skip(
            node="impact",
            provider="deepseek",
            model=llm_ask._model_for("DEEPSEEK_MODEL",
                                     llm_ask.DEFAULT_DEEPSEEK_MODEL,
                                     purpose="impact"),
            review_id=review_id,
            outcome="budget_exceeded",
            error_detail=f"impact budget exhausted (limit={limit})",
        )
        return {"status": "budget_exceeded",
                "message": "本合同的连带影响分析次数已达上限"}

    api_key = llm_ask._deepseek_key()
    trace_id = uuid.uuid4().hex[:16]
    llm_call = llm_call or _default_llm_call
    degraded: Optional[_Degraded] = None
    findings: Optional[list[dict[str, Any]]] = None
    if not api_key:
        degraded = _Degraded("no_llm_key")
    else:
        system = impact_prompts.build_system()
        user = impact_prompts.build_user(draft_text, excerpts)
        try:
            with llm_call_log.record_node(
                "impact", review_id=review_id,
                prompt_version=impact_prompts.PROMPT_VERSION,
                trace_id=trace_id,
            ):
                raw = llm_call(api_key, system, user)
            findings = _parse_findings(raw, len(excerpts))
        except _Degraded as exc:
            degraded = exc
        except Exception as exc:  # noqa: BLE001
            logger.warning("impact llm error: %s", exc)
            degraded = _Degraded("llm_error")

    if degraded is not None or findings is None:
        reason = degraded.reason if degraded else "unknown"
        _save_impact_report(review_id, item_id, draft_id, trace_id, {},
                            "degraded", reason)
        return {"status": "degraded", "reason": reason,
                "message": "连带影响分析失败，请稍后重试（已留痕，不影响审查结论）"}

    report = {
        "covered": True,
        "excerpts": excerpts,
        "findings": findings,
        "selected_draft_id": draft_id,
        "selected_stance": selected_stance,
        "declaration": ("以下为一致性定位说明，不构成法律意见；"
                        "逐例语义核对以真实文本为准。"),
        "notice": impact_prompts.RE_REVIEW_NOTICE,
    }
    _save_impact_report(review_id, item_id, draft_id, trace_id, report, "ok")
    return {"status": "ok", "report": report, "trace_id": trace_id,
            "remaining": remaining_budget(review_id, "impact", limit)}
