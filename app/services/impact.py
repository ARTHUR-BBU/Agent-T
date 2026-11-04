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

from app.prompts import impact_prompts, revision_prompts
from app.services import evidence as evidence_module
from app.services import llm_ask, llm_call_log, llm_client, store as store_module
from app.services.revision import (
    _connect,
    _db_path,
    _Degraded,
    consume_budget,
    impact_limit_from_env,
    remaining_budget,
    revision_limit_from_env,
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


# 摘录窗口：命中关键词前后各 40 字符（无条款索引时的**降级**兜底窗；
# 真实入口此场景已受限未覆盖，见 analyze_impact——辅助函数兜底不绕门禁）
_EXCERPT_WINDOW = 40
# 条款全段摘录上限（B7-4：同条款合并扩窗到条款全段，但「全段」不是
# 无限制——超长按截断段出多条，切点落在句末标点语义完整处）
_EXCERPT_MAX_CHARS = 300


def _split_by_cap(seg_text: str, base: int,
                  cap: int) -> list[tuple[int, int]]:
    """把 [base, base+len) 按上限切为若干 (lo, hi) 段（全文绝对坐标），
    切点优先落在句末标点（。；！）之后的语义完整处；单句超长则硬切。"""
    cuts: list[tuple[int, int]] = []
    lo = 0
    n = len(seg_text)
    while lo < n:
        hi = min(lo + cap, n)
        if hi < n:
            best = -1
            for i in range(hi - 1, lo, -1):
                if seg_text[i] in "。；！":
                    best = i + 1
                    break
            if best > lo:
                hi = best
        cuts.append((base + lo, base + hi))
        lo = hi
    return cuts


def _clip_ranges(lo: int, hi: int, pos: int,
                 ranges: list[tuple[int, int]]) -> tuple[int, int]:
    """摘录窗在排除区间边界裁剪：不把被改条款原文渗进「其他条款」摘录
    （否则摘录挂着原条款尾巴，定位语义被污染）。"""
    for rlo, rhi in ranges:
        if rlo < hi and rhi > lo:  # 窗口与排除区间相交
            if pos >= rhi:
                lo = max(lo, rhi)
            elif pos < rlo:
                hi = min(hi, rlo)
    return lo, hi


def find_related_clauses(
    text: str,
    item_id: str,
    exclude_positions: Optional[list[tuple[int, int]]] = None,
    clause_index: Optional[dict[str, Any]] = None,
    exclude_clause_ids: Optional[set[str]] = None,
) -> list[dict[str, Any]]:
    """按词族在合同全文检索**其他条款**的摘录（B7-4 同条款合并版）。

    排除被改条款自身（codex #154 复批 3）：命中点落在被改条款的证据坐标
    区间内、或按条款索引定位到被改条款的 clause_id，则不计入——否则会把
    原条款自己当成「其他相关条款」误报已覆盖。

    B7-4 同条款合并（老钱裁定，根治 I1「半截话+下一条款残片」近重复）：
    - 同一条款多个命中 → **合并为一条**，摘录窗=条款全段；keyword 改为
      **数组**（合并命中词逐一保留，多重性不丢）；position 取条款起始
    - 条款超长超 _EXCERPT_MAX_CHARS → 按截断段出多条（切点句末标点
      语义完整处）+ excerpt_truncated 标注（物理限制非语义判断）
    - 无条款索引（或命中点在条款结构之外）→ **降级**维持旧桶去重窗
      （仅辅助兜底；真实入口此场景已受限未覆盖，不绕门禁）
    - 不同条款各自保留（现状）"""
    families = _load_families()
    keywords = families.get(item_id) or []
    ranges = exclude_positions or []
    exclude_ids = exclude_clause_ids or set()

    # 第一遍：收集全部候选命中（排除逻辑不变）
    hits: list[tuple[str, int, Optional[str]]] = []
    for kw in keywords:
        start = 0
        while True:
            pos = text.find(kw, start)
            if pos < 0:
                break
            start = pos + len(kw)
            if any(lo <= pos < hi for lo, hi in ranges):
                continue
            cid: Optional[str] = None
            if clause_index:
                cid = evidence_module._clause_at(clause_index, pos)
                if cid and cid in exclude_ids:
                    continue
            hits.append((kw, pos, cid))

    excerpts: list[dict[str, Any]] = []

    def _fallback(hs: list[tuple[str, int, Optional[str]]]) -> None:
        """降级桶去重窗（现状行为；keyword 统一为数组口径）。"""
        seen: set[int] = set()
        for kw, pos, _cid in hs:
            bucket = pos // _EXCERPT_WINDOW
            if bucket in seen:
                continue
            seen.add(bucket)
            lo = max(0, pos - _EXCERPT_WINDOW)
            hi = min(len(text), pos + len(kw) + _EXCERPT_WINDOW)
            lo, hi = _clip_ranges(lo, hi, pos, ranges)
            excerpt = text[lo:hi].replace("\n", " ").strip()
            if excerpt:
                excerpts.append({
                    "index": len(excerpts),
                    "keyword": [kw],
                    "position": pos,
                    "excerpt": excerpt,
                })

    if not clause_index:
        _fallback(hits)
        return excerpts

    # 有条款索引：按条款归组（cid=None 的散命中走降级窗）
    by_id = {c.get("id"): c for c in (clause_index.get("clauses") or [])}
    clause_hits: dict[str, list[tuple[str, int]]] = {}
    loose: list[tuple[str, int, Optional[str]]] = []
    for kw, pos, cid in hits:
        if cid and cid in by_id:
            clause_hits.setdefault(cid, []).append((kw, pos))
        else:
            loose.append((kw, pos, cid))
    for cid in sorted(clause_hits, key=lambda c: int(
            by_id[c].get("start") or 0)):
        meta = by_id[cid]
        clo = int(meta.get("start") or 0)
        chi = int(meta.get("end") or 0)
        if chi <= clo:
            continue
        segments = (_split_by_cap(text[clo:chi], clo, _EXCERPT_MAX_CHARS)
                    if chi - clo > _EXCERPT_MAX_CHARS else [(clo, chi)])
        multi = len(segments) > 1
        for slo, shi in segments:
            seg_kws = [kw for kw, pos in clause_hits[cid]
                       if slo <= pos < shi]
            if not seg_kws:
                continue
            lo, hi = _clip_ranges(slo, shi, slo, ranges)
            excerpt = text[lo:hi].replace("\n", " ").strip()
            if not excerpt:
                continue
            entry: dict[str, Any] = {
                "index": len(excerpts),
                "keyword": seg_kws,
                "position": clo,
                "excerpt": excerpt,
                "clause_id": cid,
            }
            if multi:
                entry["excerpt_truncated"] = True
            excerpts.append(entry)
    _fallback(loose)
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
    "payload TEXT NOT NULL, status TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',"
    "stance TEXT NOT NULL DEFAULT '')")


def _migrate_impact_reports(conn: sqlite3.Connection) -> None:
    """幂等迁移（B7-3a）：老表补 stance 列（绑定版别）。沿用账本「只增
    不删、检缺列再 ALTER」纪律；历史行 stance 为空 → 前端按「历史记录
    （版别未登记）」渲染，不硬配当前选择。生产现 0 行，迁移零包袱。
    **只在写入路径调用**（_save_impact_report）；GET 读侧不改 schema
    （codex #163 复批 4）。并发竞态（两连接同时过 PRAGMA 检查）由
    duplicate-column 吞掉兜底。"""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(impact_reports)")}
    if "stance" not in cols:
        try:
            conn.execute(
                "ALTER TABLE impact_reports ADD COLUMN stance"
                " TEXT NOT NULL DEFAULT ''")
        except sqlite3.OperationalError as e:
            if "duplicate column" not in str(e).lower():
                raise


def _save_impact_report(review_id: str, item_id: str, draft_id: str,
                        trace_id: str, payload: dict[str, Any],
                        status: str, reason: str = "",
                        stance: str = "") -> None:
    with _connect() as conn:
        conn.execute(_IMPACT_REPORTS_DDL)
        _migrate_impact_reports(conn)
        conn.execute(
            "INSERT INTO impact_reports (id, review_id, item_id, draft_id,"
            " created_at, trace_id, payload, status, reason, stance)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (uuid.uuid4().hex[:16], review_id, item_id, draft_id, time.time(),
             trace_id, json.dumps(payload, ensure_ascii=False), status, reason,
             stance))


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
                "uncovered_code": "missing_source",
                "notice": impact_prompts.RE_REVIEW_NOTICE,
                "findings": [],
            }
            _save_impact_report(review_id, item_id, draft_id, "", report,
                                "uncovered", stance=selected_stance)
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
            "uncovered_code": ("missing_source" if not contract_text
                               else "no_match"),
            "uncovered_reason": (
                "未在合同全文检索到该条款之外可能与草案联动的其他条款"
                "（不代表没有冲突）"
                if contract_text else
                "审查单缺少合同全文，无法定位相关条款簇（不代表没有冲突）"),
            "notice": impact_prompts.RE_REVIEW_NOTICE,
            "findings": [],
        }
        _save_impact_report(review_id, item_id, draft_id, "", report,
                            "uncovered", stance=selected_stance)
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
                            "degraded", reason, stance=selected_stance)
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
    _save_impact_report(review_id, item_id, draft_id, trace_id, report, "ok",
                        stance=selected_stance)
    return {"status": "ok", "report": report, "trace_id": trace_id,
            "remaining": remaining_budget(review_id, "impact", limit)}


# ---- B7-3a：聚合状态接口（页面刷新只读恢复；纯读库，零 AI、零扣额） ----

def _uncovered_code(payload: dict[str, Any]) -> str:
    """uncovered 机器可读细分（线框 §3 态F1/F2）：no_match=检索过词族
    未命中；missing_source=缺全文/无法定位（含保守受限）。"""
    code = payload.get("uncovered_code")
    if code in ("no_match", "missing_source"):
        return str(code)
    text = str(payload.get("uncovered_reason") or "")
    if "缺少" in text or "无法定位" in text:
        return "missing_source"
    return "no_match"


def _findings_output_check(findings: list) -> tuple[list, int, list[int]]:
    """findings 说明文本输出检查（施工卡 §五A：格式合格≠内容安全）。
    禁语表同构检查——签约诱导类措辞若渗入说明，该条隐藏+计数+**记编号**
    （codex #165 复批 P1：报告要按编号出示被隐藏说明对应的合同原文摘录，
    不能靠可见数+隐藏数反推）。合同原文摘录（excerpts）是证据，原样
    不过滤。返回 (可见说明, 隐藏数, 隐藏编号列表)。"""
    visible: list[dict[str, Any]] = []
    hidden = 0
    hidden_indexes: list[int] = []
    for f in findings or []:
        note = str(f.get("note") or "")
        if any(pat in note for pat in revision_prompts.BANNED_PATTERNS):
            hidden += 1
            idx = f.get("index")
            if isinstance(idx, int):
                hidden_indexes.append(idx)
            continue
        visible.append(f)
    return visible, hidden, hidden_indexes


def revision_state(store: Any, review_id: str, item_id: str) -> dict[str, Any]:
    """聚合状态接口（GET .../revision/state 的服务函数，B7-3a §四A）。

    一次返回页面刷新恢复所需全部态：drafts 倒序（含选定标注）、当前
    选定 (draft_id, stance)、牵连最新一次状态（绑定版别+legacy 标记+
    findings 输出检查）、剩余额度。**纯读库：零 AI 调用、零扣额、
    不建表不迁移不改 schema**（codex #163 复批 4——建表/迁移只在写入
    路径；表未建/老表缺列时只读退化，捕获 OperationalError 给空态）。

    牵连记录取「**当前选定版别**的最新一次」（codex #163 复批 2）：
    A 查过、切 B 查过、再切回 A → 返回 A 自己的结果，不把 B 标过期充数。
    当前选定无绑定结果时，退到最近一条无版别老记录（legacy 另标不猜）；
    绑定其他版别的记录不返回（那是别人版本的账，不是本版的）。"""
    review = store.get(review_id)
    if review is None:
        return {"status": "not_found", "reason": "review_not_found"}
    item = next((i for i in review.get("items") or []
                 if i.get("id") == item_id), None)
    if item is None:
        return {"status": "not_found", "reason": "item_not_found"}

    sel_draft = sel_stance = None
    drows: list = []
    irow = None
    # 裸连接（复批 4）：revision._connect 自带建表 DDL，GET 侧绝不能走它——
    # 这里 sqlite3 直连同库，只 SELECT/PRAGMA，零 schema 变更零业务写入
    conn = sqlite3.connect(_db_path())
    try:
        # 纯读三连：缺表/缺列一律 OperationalError → 只读退化，不动 schema
        try:
            srow = conn.execute(
                "SELECT draft_id, stance FROM selected_drafts"
                " WHERE review_id=? AND item_id=?",
                (review_id, item_id)).fetchone()
            if srow:
                sel_draft, sel_stance = srow[0], srow[1]
        except sqlite3.OperationalError:
            pass  # selected_drafts 未建（从未选过）→ 未选定
        try:
            drows = conn.execute(
                "SELECT id, created_at, status, payload FROM revision_drafts"
                " WHERE review_id=? AND item_id=?"
                " ORDER BY created_at DESC, id DESC",
                (review_id, item_id)).fetchall()
        except sqlite3.OperationalError:
            drows = []  # revision_drafts 未建（从未生成）→ 空
        try:
            cols = {r[1] for r in
                    conn.execute("PRAGMA table_info(impact_reports)")}
        except sqlite3.OperationalError:
            cols = set()
        if cols and "stance" in cols:
            try:
                if sel_draft:
                    # 当前选定版别自己的最新一次（复批 2 核心语义）
                    irow = conn.execute(
                        "SELECT draft_id, stance, status, payload, reason,"
                        " created_at FROM impact_reports"
                        " WHERE review_id=? AND item_id=?"
                        " AND draft_id=? AND stance=?"
                        " ORDER BY created_at DESC, id DESC LIMIT 1",
                        (review_id, item_id, sel_draft, sel_stance)).fetchone()
                if irow is None:
                    # 无绑定结果 → 最近一条无版别老记录（legacy 另标不猜）
                    irow = conn.execute(
                        "SELECT draft_id, stance, status, payload, reason,"
                        " created_at FROM impact_reports"
                        " WHERE review_id=? AND item_id=? AND stance=''"
                        " ORDER BY created_at DESC, id DESC LIMIT 1",
                        (review_id, item_id)).fetchone()
            except sqlite3.OperationalError:
                irow = None
        elif cols:
            # 老表缺 stance 列：只读退化，整行按 legacy 读（不迁移不 ALTER）
            try:
                irow = conn.execute(
                    "SELECT draft_id, NULL, status, payload, reason,"
                    " created_at FROM impact_reports"
                    " WHERE review_id=? AND item_id=?"
                    " ORDER BY created_at DESC, id DESC LIMIT 1",
                    (review_id, item_id)).fetchone()
            except sqlite3.OperationalError:
                irow = None
        # cols 为空 = impact_reports 未建 → impact 保持 None
        # 剩余额度（纯读）：revision.remaining_budget 走 _connect（带建表
        # DDL），GET 侧同裸连接直查，缺表按未消耗计（复批 4）
        used_rev = used_imp = 0
        try:
            brow = conn.execute(
                "SELECT revision_used, impact_used FROM revision_budget"
                " WHERE review_id=?", (review_id,)).fetchone()
            if brow:
                used_rev, used_imp = brow[0], brow[1]
        except sqlite3.OperationalError:
            pass  # revision_budget 未建（从未触发过生成/分析）
    finally:
        conn.close()
    remaining = {
        "revision": max(0, revision_limit_from_env() - used_rev),
        "impact": max(0, impact_limit_from_env() - used_imp),
    }

    drafts: list[dict[str, Any]] = []
    for did, created, status, payload in drows:
        entry: dict[str, Any] = {
            "draft_id": did, "created_at": created, "status": status,
            "is_selected": bool(sel_draft == did),
            "selected_stance": sel_stance if sel_draft == did else None,
        }
        if status == "ok":
            try:
                pl = json.loads(payload)
            except ValueError:
                entry["status"] = "degraded"
            else:
                entry.update({
                    "versions": pl.get("versions") or [],
                    "rationale": pl.get("rationale") or "",
                    "assumptions": pl.get("assumptions") or [],
                    "declaration": pl.get("declaration") or "",
                })
        drafts.append(entry)

    selected = ({"draft_id": sel_draft, "stance": sel_stance}
                if sel_draft else None)

    impact: Optional[dict[str, Any]] = None
    if irow is not None:
        i_draft, i_stance, i_status, i_payload, i_reason, i_created = irow
        i_stance = i_stance or ""
        try:
            pl = json.loads(i_payload) if i_payload else {}
        except ValueError:
            pl = {}
        entry = {
            "bound": {"draft_id": i_draft, "stance": i_stance or None},
            "state": i_status,
            "legacy": not bool(i_stance),
            "created_at": i_created,
        }
        if i_status == "uncovered":
            entry["uncovered_reason"] = str(
                pl.get("uncovered_reason") or i_reason or "")
            entry["uncovered_code"] = _uncovered_code(pl)
        elif i_status == "ok":
            findings, hidden, hidden_indexes = _findings_output_check(
                pl.get("findings") or [])
            entry.update({
                "findings": findings,
                "hidden_findings": hidden,
                # 被隐藏说明的具体编号（codex #165 复批 P1）：报告按编号
                # 出示对应合同原文摘录+「说明已隐藏」，不靠总数反推
                "hidden_indexes": hidden_indexes,
                "notice": pl.get("notice") or "",
                "excerpts": pl.get("excerpts") or [],
                "covered": bool(pl.get("covered", True)),
                # 绑定与当前选定不一致 → 前端按「已过期」渲染（线框态H），
                # 不顶替当前选定版的检查结果
                "stale": not (i_draft == sel_draft and i_stance == sel_stance),
            })
        elif i_status == "degraded":
            entry["reason"] = i_reason or str(pl.get("reason") or "")
        impact = entry

    return {"status": "ok", "item_id": item_id, "drafts": drafts,
            "selected": selected, "impact": impact, "remaining": remaining}


# ---- B7-3b：报告草案章节数据组装（§五A 规则；纯读库零 AI 零扣额） ----

def _version_hits_banned(ver: dict[str, Any], batch: dict[str, Any]) -> bool:
    """版别整体禁语检查（§五A：草案文本/理由/假设随所属版整体处置——
    任一处命中，该版整体不入报告，如实标注，绝不掐头留尾）。
    理由/假设/免责声明在批次层（state 组装口径），文本/适用在版别层。"""
    texts = [str(ver.get("text") or ""), str(ver.get("note") or ""),
             str(batch.get("rationale") or "")]
    texts += [str(a) for a in batch.get("assumptions") or []]
    return any(any(pat in t for pat in revision_prompts.BANNED_PATTERNS)
               for t in texts)


def revision_report_section(store: Any, review_id: str) -> list[dict[str, Any]]:
    """报告「AI 修改建议（AI 起草辅助）」章节数据（B7-3b §五A）。

    纯读库组装：逐项复用 revision_state 语义（绑定版别最新牵连/
    legacy 不硬配/findings 输出检查），**零 LLM 调用、零扣额、零
    schema 变更**。渲染规则：
    - 无原文摘句（quote）的检查项不进章节（无对照锚）
    - 无任何修订活动（无草案/无选定/无牵连）的项不进章节；全卷无
      修订活动返回 []——章节整体不渲染，M4 报告字节契约不受影响
    - 未选定 → 如实「未选定修改建议」，不展开候选文本（不虚构）
    - 选定版命中禁语 → 该版整体不入报告，banned=True 如实标注
    - 牵连数据直接沿用 state 组装（findings 已过滤+hidden_findings
      计数），报告层据此明示「结果不完整」"""
    review = store.get(review_id)
    if review is None:
        return []
    out: list[dict[str, Any]] = []
    for item in review.get("items") or []:
        if not item.get("quote"):
            continue  # 无原文对照锚不进章节
        item_id = str(item.get("id") or "")
        s = revision_state(store, review_id, item_id)
        if s.get("status") != "ok":
            continue
        drafts = s.get("drafts") or []
        selected = s.get("selected")
        imp = s.get("impact")
        if not drafts and not selected and not imp:
            continue  # 无修订活动：不占报告篇幅
        entry: dict[str, Any] = {
            "item_id": item_id,
            "item_name": str(item.get("name") or ""),
            "item_status": str(item.get("status") or ""),
            "quote": str(item.get("quote") or ""),
            "draft": None,
            "impact": imp,
        }
        if selected:
            batch = next((d for d in drafts
                          if d.get("draft_id") == selected.get("draft_id")),
                         None)
            ver = None
            if batch is not None and batch.get("status") == "ok":
                ver = next((v for v in batch.get("versions") or []
                            if v.get("stance") == selected.get("stance")),
                           None)
            if batch is None or ver is None:
                # 选定记录指向的批次/版别缺失（降级批或数据不全）：如实标注
                entry["draft"] = {"banned": False, "missing": True,
                                  "stance": selected.get("stance") or ""}
            else:
                banned = _version_hits_banned(ver, batch)
                entry["draft"] = {
                    "banned": banned,
                    "missing": False,
                    "stance": str(ver.get("stance") or ""),
                    # 命中禁语：内容整体不入报告（宁缺勿污），只留标注
                    "text": "" if banned else str(ver.get("text") or ""),
                    "note": "" if banned else str(ver.get("note") or ""),
                    "rationale": "" if banned else str(
                        batch.get("rationale") or ""),
                    "assumptions": ([] if banned else
                                    [str(a) for a in
                                     batch.get("assumptions") or []]),
                    "declaration": "" if banned else str(
                        batch.get("declaration") or ""),
                }
        out.append(entry)
    return out
