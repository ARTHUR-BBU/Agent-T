"""异议层 API（外审批 3：从 routes.py 按领域拆出；URL 与语义不变）。"""
from __future__ import annotations

import logging
import hashlib
import json

from fastapi import APIRouter, HTTPException

from app.api.deps import require_done_row
from app.api.schemas import ObjectionInfo
from app.services.evidence import compose_decision, derive_claims_view
from app.services.store import store

router = APIRouter()  # 前缀由聚合 router（/api）提供，勿重复

logger = logging.getLogger(__name__)


@router.get("/objection-proposals/capability")
def objection_proposals_capability():
    return {"available": True}


def _proposal_views(row: dict) -> list[dict]:
    """从当前证据视图生成提案，失效的采纳记录不得当作可评审提案。"""
    view, _, _ = derive_claims_view(row)
    out = []
    for ob in (view.get("objections") or {}).get("objections") or []:
        decision = ob.get("decision") or {}
        if (not ob.get("adopted") or not ob.get("accepted")
                or decision.get("consistency") != "consistent"
                or not ob.get("claim_id") or not ob.get("claim_content_hash")):
            continue
        out.append(ob)
    return out


def _proposal_snapshot(ob: dict) -> str:
    """绑定主张、反证以及支持和被反驳的证据，防止旧评审套用新材料。"""
    refs = sorted((str(ref.get("evidence_id") or ""), str(ref.get("relation") or ""))
                  for ref in ob.get("evidence_refs") or [] if isinstance(ref, dict))
    data = [ob.get("claim_id"), ob.get("claim_content_hash"), ob.get("item_id"),
            ob.get("rule_id"), ob.get("direction"), ob.get("counter_evidence"),
            ob.get("counter_evidence_status"), ob.get("rebuts_status"), refs]
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, separators=(",", ":"))
                          .encode("utf-8")).hexdigest()


@router.get("/objection-proposals")
def list_objection_proposals():
    """提案跟随审查记录的保留期，既不复制合同也不制造永久档案。"""
    proposals = []
    summaries = store.list_reviews(limit=1001)
    for summary in summaries[:1000]:
        row = store.get(summary["id"])
        if not row:
            continue
        for ob in _proposal_views(row):
            proposals.append({
                "review_id": summary["id"], "filename": summary["filename"],
                "claim_id": ob["claim_id"], "claim_content_hash": ob["claim_content_hash"],
                "snapshot_hash": _proposal_snapshot(ob),
                "item_id": ob.get("item_id", ""), "rule_id": ob.get("rule_id"),
                "proposal": ob.get("proposal", ""), "reasoning": ob.get("legal_reasoning", ""),
                "quote": ob.get("quote", ""), "counter_evidence": ob.get("counter_evidence", ""),
                "reviews": ob.get("proposal_reviews") or [],
            })
    return {"proposals": proposals, "truncated": len(summaries) > 1000, "ttl_seconds": store.ttl_seconds}


@router.post("/review/{review_id}/objection-proposals/review")
def review_objection_proposal(review_id: str, body: dict):
    """记录人工意见，不改当前审查、不发布规则；写前校验主张快照。"""
    from datetime import datetime, timezone

    claim_id = body.get("claim_id")
    content_hash = body.get("claim_content_hash")
    snapshot_hash = body.get("snapshot_hash")
    verdict = body.get("verdict")
    reviewer = body.get("reviewer")
    note = body.get("note")
    revision = body.get("revision")
    if (not isinstance(claim_id, str) or not claim_id or len(claim_id) > 100
            or not isinstance(content_hash, str) or not content_hash or len(content_hash) > 100
            or not isinstance(snapshot_hash, str) or len(snapshot_hash) != 64
            or verdict not in ("approved", "rejected")
            or not isinstance(reviewer, str) or not 1 <= len(reviewer.strip()) <= 80
            or not isinstance(note, str) or not 1 <= len(note.strip()) <= 2000
            or isinstance(revision, bool) or not isinstance(revision, int) or revision < 0):
        raise HTTPException(status_code=422, detail="请填写评审人、评审意见和有效的提案信息")

    def change(row: dict) -> None:
        if row.get("status") != "done":
            raise HTTPException(status_code=409, detail="审查尚未完成")
        matches = [ob for ob in _proposal_views(row) if ob.get("claim_id") == claim_id
                   and ob.get("claim_content_hash") == content_hash]
        if len(matches) != 1 or _proposal_snapshot(matches[0]) != snapshot_hash:
            raise HTTPException(status_code=409, detail="提案已变化或证据失效，请刷新后核对")
        raw = (row.get("objections") or {}).get("objections") or []
        candidates = [ob for ob in raw if isinstance(ob, dict)
                      and (ob.get("decision") or {}).get("claim_id") == claim_id
                      and (ob.get("decision") or {}).get("claim_content_hash") == content_hash]
        if len(candidates) != 1:
            raise HTTPException(status_code=409, detail="提案记录不唯一，请刷新后核对")
        history = candidates[0].get("proposal_reviews") or []
        if not isinstance(history, list) or len(history) != revision:
            raise HTTPException(status_code=409, detail="已有新的评审记录，请刷新后核对")
        if len(history) >= 100:
            raise HTTPException(status_code=409, detail="评审记录已达上限")
        candidates[0]["proposal_reviews"] = [*history, {
            "verdict": verdict, "reviewer": reviewer.strip(), "note": note.strip(),
            "claim_id": claim_id, "claim_content_hash": content_hash, "snapshot_hash": snapshot_hash,
            "at": datetime.now(timezone.utc).isoformat(),
        }]

    if store.mutate(review_id, change) is None:
        raise HTTPException(status_code=404, detail="审查记录不存在或已过期")
    return {"ok": True, "rule_published": False}


@router.post("/review/{review_id}/objections/adopt", response_model=ObjectionInfo)
def adopt_objection(review_id: str, body: dict):
    """采纳异议为规则改进提案（阶段 3.2）。只标 adopted 键 + 挂决定记录；
    规则 items 档位不动（Design B：快照比对，不一致 500）。提案文本由前端
    复制给人评审，真正的规则变更走版本化修订通道（铁律 3）。"""
    row = require_done_row(review_id)
    raw_index = body.get("index")
    # 严格整数：bool/浮点/非整数字符串一律 422（0.9→0、true→1 之类 lax 转换不开口；
    # int("0.9") 本就抛 ValueError，无需额外判断）
    if isinstance(raw_index, bool) or not isinstance(raw_index, (int, str)):
        raise HTTPException(status_code=422, detail="index 必须是整数")
    try:
        index = int(str(raw_index).strip())
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="index 必须是整数")
    def change(row: dict) -> None:
        if row.get("status") != "done":
            raise HTTPException(status_code=409, detail="审查尚未完成")
        obj_row = row.get("objections")
        if not isinstance(obj_row, dict) or not obj_row.get("available"):
            raise HTTPException(status_code=404, detail="该审查没有异议数据")
        obs = obj_row.get("objections") or []
        if not (0 <= index < len(obs)):
            raise HTTPException(status_code=404, detail="异议序号不存在")
        # 只受理已过五要件的条目：拒收留痕条目（accepted=False）不可采纳
        if not obs[index].get("accepted"):
            raise HTTPException(status_code=422, detail="该异议未通过受理校验，不可采纳")
        # 2c（§3.3，审计 P1-2）：先写时派生 claim_id——无有效主张编号直接
        # 拒绝采纳，绝不留下「页面已采纳、档案无决定」的假状态
        view, _w, _cw = derive_claims_view({
            "text": row.get("text") or "",
            "document_version": row.get("document_version") or "",
            "rule_pack": row.get("rule_pack"),
            "items": row.get("items") or [],
            "objections": obj_row,
        })
        vobs = (view.get("objections") or {}).get("objections") or []
        claim_id = (vobs[index] if index < len(vobs) else {}).get("claim_id") or ""
        if not claim_id:
            raise HTTPException(
                status_code=422,
                detail="该异议无有效主张编号（证据不合格），不能采纳为正式决定",
            )
        vob = vobs[index]
        # 快照规则档位，写入后对照（Design B）
        status_snap = {
            str(i.get("id")): i.get("status")
            for i in (row.get("items") or [])
            if isinstance(i, dict) and i.get("id")
        }
        from datetime import datetime, timezone

        decision = compose_decision(
            document_version=row.get("document_version") or "",
            claim_id=claim_id,
            claim_content_hash=vob.get("claim_content_hash") or "",
            evidence_ids=[r.get("evidence_id") for r in vob.get("evidence_refs") or []],
            decision_type="objection_adopt",
            choice="adopted",
            decided_at=datetime.now(timezone.utc).isoformat(),
            existing=(
                obs[index].get("decision")
                if isinstance(obs[index].get("decision"), dict) else None
            ),
        )
        obs[index] = {**obs[index], "adopted": True, "decision": decision}
        row["objections"] = {**obj_row, "objections": obs}
        after = row
        status_after = {
            str(i.get("id")): i.get("status")
            for i in (after.get("items") or [])
            if isinstance(i, dict) and i.get("id")
        }
        if status_snap != status_after:
            logger.error("Design B violation on objections/adopt review_id=%s", review_id)
            raise HTTPException(status_code=500, detail="内部错误：规则档位不得被异议改动")
    row = store.mutate(review_id, change)
    if row is None:
        raise HTTPException(status_code=404, detail="审查记录不存在或已过期")
    new_obj = row.get("objections") or {}
    # 2c（§2.5-④）：响应走统一流水线组装——不再用 store 原件（claim/decision
    # 一致性字段在派生视图里才完整）
    resp_view, _w, _cw = derive_claims_view({
        "text": row.get("text") or "",
        "document_version": row.get("document_version") or "",
        "rule_pack": row.get("rule_pack"),
        "items": row.get("items") or [],
        "objections": new_obj,
    })
    return ObjectionInfo(**resp_view["objections"])
