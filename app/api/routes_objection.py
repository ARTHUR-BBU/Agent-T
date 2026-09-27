"""异议层 API（外审批 3：从 routes.py 按领域拆出；URL 与语义不变）。"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from app.api.deps import require_done_row
from app.api.schemas import ObjectionInfo
from app.services import verify as verify_service
from app.services.evidence import compose_decision, derive_claims_view
from app.services.store import store

router = APIRouter()  # 前缀由聚合 router（/api）提供，勿重复

logger = logging.getLogger(__name__)


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
    with verify_service.lock_for(review_id):
        row = store.get(review_id) or row
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
        store.update(review_id, objections={**obj_row, "objections": obs})
        after = store.get(review_id) or {}
        status_after = {
            str(i.get("id")): i.get("status")
            for i in (after.get("items") or [])
            if isinstance(i, dict) and i.get("id")
        }
        if status_snap != status_after:
            logger.error("Design B violation on objections/adopt review_id=%s", review_id)
            raise HTTPException(status_code=500, detail="内部错误：规则档位不得被异议改动")
        new_obj = after.get("objections") or obj_row
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
