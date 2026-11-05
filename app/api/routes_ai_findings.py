"""B7-5a「AI 发现」状态 API（spec-b75a-ai-findings.md）。"""
from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import require_done_row
from app.api.schemas import (
    AiFindingStatusRequest,
    AiFindingStatusResponse,
    ConfirmRequest,
)
from app.api.routes_verify import confirm_core
from app.services import ai_findings as ai_findings_service
from app.services.store import store

router = APIRouter()  # 前缀由聚合 router（/api）提供，勿重复


@router.post(
    "/review/{review_id}/ai-finding/status",
    response_model=AiFindingStatusResponse,
)
def set_ai_finding_status(review_id: str, body: AiFindingStatusRequest):
    """统一发现状态入口：确认（物化）/争议（停用）/已阅（留痕）。

    派生发现（带 question_id）的确认/争议先走 confirm 链（compose_decision
    决定记录+Design B 校验+证据索引重建），再物化/停用检查项——
    同一发现只有一条确认链。"""
    require_done_row(review_id)
    row = store.get(review_id) or {}
    # 键解析与 GET 页面同源：claim_id 是读路径派生属性，须在归一化视图上算
    from app.services.evidence import derive_claims_view
    norm_row, _w, _cw = derive_claims_view(row)
    finding = next(
        (f for f in ai_findings_service.review_findings(norm_row).get("findings") or []
         if f["finding_key"] == body.finding_key), None)
    if finding is None:
        return AiFindingStatusResponse(ok=False, error="finding_not_found")

    if finding.get("question_id") and body.status in ("confirmed", "disputed"):
        resp = confirm_core(review_id, ConfirmRequest(
            question_id=str(finding["question_id"]),
            choice="confirm" if body.status == "confirmed" else "dispute",
            human_note="",
        ))
        if not resp.ok:
            return AiFindingStatusResponse(ok=False, error=resp.error)

    out = ai_findings_service.set_status(
        store, review_id, finding_key=body.finding_key, status=body.status)
    if out.get("status") != "ok":
        return AiFindingStatusResponse(ok=False, error=out.get("error"))
    return AiFindingStatusResponse(
        ok=True, finding_status=body.status,
        item_id=out.get("item_id"),
        materialized=bool(out.get("materialized")))
