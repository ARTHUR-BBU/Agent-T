"""B7-5a「AI 发现」状态 API（spec-b75a-ai-findings.md）。"""
from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import require_done_row
from app.api.schemas import (
    AiFindingStatusRequest,
    AiFindingStatusResponse,
    ConfirmRequest,
)
from app.api.routes_verify import _confirm_under_lock
from app.services import ai_findings as ai_findings_service
from app.services.store import store
from app.services.verify import lock_for

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

    # 仅事实类派生发现走 confirm 决定链（codex 五轮 P2）：观点争议只是
    # 反馈、不产生 claim 决定记录——观点全部走状态表权威，恢复已阅才能
    # 盖过问题行旧态
    if finding.get("question_id") and finding.get("fact_type") \
            and body.status in ("confirmed", "disputed"):
        # codex 六轮 P1：确认决定+状态保存+检查项更新放进**同一次**持锁
        # ——确认/争议两条请求交错时整体串行，不再出现「决定已争议、
        # 检查项仍有效」的中间态（此前两次持锁中间放行其他请求）
        with lock_for(review_id):
            resp = _confirm_under_lock(review_id, ConfirmRequest(
                question_id=str(finding["question_id"]),
                choice="confirm" if body.status == "confirmed" else "dispute",
                human_note="",
            ))
            if not resp.ok:
                return AiFindingStatusResponse(ok=False, error=resp.error)
            out = ai_findings_service.set_status_locked(
                store, review_id, finding_key=body.finding_key,
                status=body.status)
    else:
        out = ai_findings_service.set_status(
            store, review_id, finding_key=body.finding_key,
            status=body.status)
    if out.get("status") != "ok":
        return AiFindingStatusResponse(ok=False, error=out.get("error"))
    return AiFindingStatusResponse(
        ok=True, finding_status=body.status,
        item_id=out.get("item_id"),
        materialized=bool(out.get("materialized")))
