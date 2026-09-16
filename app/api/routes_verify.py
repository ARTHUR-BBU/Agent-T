"""A6 有界核验 API（外审批 3：从 routes.py 按领域拆出；URL 与语义不变）。"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from app.api.deps import require_done_row
from app.api.schemas import ConfirmRequest, ConfirmResponse, ReverifyRequest, ReverifyResponse, VerifyBudgetInfo, VerifyInfo
from app.services import verify as verify_service
from app.services.store import store

router = APIRouter()  # 前缀由聚合 router（/api）提供，勿重复

logger = logging.getLogger(__name__)


def pack_verify(raw: dict | None) -> VerifyInfo | None:
    """把 store.verify 打成 API 形状（附 budget 快照）。"""
    if not raw or not isinstance(raw, dict):
        return None
    try:
        info = verify_service.VerifyInfo.model_validate(raw)
    except Exception:  # noqa: BLE001
        return None
    payload = info.model_dump()
    payload["budget"] = info.budget_snapshot()
    return VerifyInfo(**payload)


# routes.py 的向后兼容别名（外部引用零破坏）
_pack_verify = pack_verify


@router.get("/review/{review_id}/verify", response_model=VerifyInfo)
def get_verify(review_id: str):
    """列出待确认问题 + 剩余预算（A6）。"""
    row = store.get(review_id)
    if not row:
        raise HTTPException(status_code=404, detail="审查记录不存在")
    packed = pack_verify(row.get("verify"))
    if packed is None:
        return VerifyInfo(
            available=False,
            reason="not_attempted",
            disclaimer="主动核查只提疑点，不改变清单规则档",
            budget=VerifyBudgetInfo(),
        )
    return packed


@router.post("/review/{review_id}/verify", response_model=VerifyInfo)
def trigger_verify(review_id: str):
    """再跑一轮有界核验（受 rounds/取证次数封顶）。不改规则档位。"""
    row = require_done_row(review_id)
    with verify_service.lock_for(review_id):
        # 门禁 P2-3：锁内重取 row——锁外快照可能与并发的 confirm 写入交错，
        # 用 stale prior 重建会把人工确认冲回 pending（confirm/reverify 均在
        # 锁内重取，唯独此处漏了）
        row = store.get(review_id) or row
        prior = row.get("verify") or {}
        try:
            out = verify_service.run_bounded_verify(
                text=row.get("text") or "",
                items=row.get("items") or [],
                quality=row.get("quality") or {},
                blind_candidates=row.get("blind_candidates") or [],
                facts=row.get("facts") or (row.get("quality") or {}).get("facts") or [],
                clause_index=row.get("clause_index"),
                document_version=row.get("document_version") or "",
                prior=prior if isinstance(prior, dict) else {},
            )
        except Exception:  # noqa: BLE001
            logger.exception("trigger verify failed review_id=%s", review_id)
            raise HTTPException(status_code=500, detail="核验失败，请稍后重试")
        store.update(review_id, verify=out)
    packed = pack_verify(out)
    assert packed is not None
    return packed


@router.post(
    "/review/{review_id}/confirm",
    response_model=ConfirmResponse,
)
def confirm_question(review_id: str, body: ConfirmRequest):
    """提交人工确认。只改 verify；规则 items 档位不动（Design B）。"""
    row = require_done_row(review_id)
    # 快照规则档位，写入后对照
    status_snap = {
        str(i.get("id")): i.get("status")
        for i in (row.get("items") or [])
        if isinstance(i, dict) and i.get("id")
    }
    with verify_service.lock_for(review_id):
        row = store.get(review_id) or row
        try:
            updated = verify_service.apply_confirmation(
                row.get("verify") or {},
                question_id=body.question_id,
                choice=body.choice,
                human_note=body.human_note or "",
                revised_quote=body.revised_quote or "",
            )
        except KeyError:
            return ConfirmResponse(ok=False, error="问题不存在")
        except ValueError as exc:
            return ConfirmResponse(ok=False, error=str(exc))
        # Design B：update 只带 verify，不带 items
        store.update(review_id, verify=updated)
        after = store.get(review_id) or {}
        after_snap = {
            str(i.get("id")): i.get("status")
            for i in (after.get("items") or [])
            if isinstance(i, dict) and i.get("id")
        }
        if after_snap != status_snap:
            logger.error("Design B violation on confirm review_id=%s", review_id)
            raise HTTPException(status_code=500, detail="内部错误：规则档位被意外改动")
    return ConfirmResponse(ok=True, verify=pack_verify(updated))


@router.post(
    "/review/{review_id}/reverify",
    response_model=ReverifyResponse,
)
def reverify_question(review_id: str, body: ReverifyRequest):
    """确认后的再核（预算内）。不改规则档位。"""
    row = require_done_row(review_id)
    status_snap = [
        (i.get("id"), i.get("status"))
        for i in (row.get("items") or [])
        if isinstance(i, dict)
    ]
    with verify_service.lock_for(review_id):
        row = store.get(review_id) or row
        try:
            updated, items_back = verify_service.recheck_question(
                text=row.get("text") or "",
                verify_state=row.get("verify") or {},
                question_id=body.question_id,
                clause_index=row.get("clause_index"),
                items_snapshot=row.get("items") or [],
            )
        except KeyError:
            return ReverifyResponse(ok=False, error="问题不存在")
        except ValueError as exc:
            return ReverifyResponse(ok=False, error=str(exc))
        except RuntimeError as exc:
            code = str(exc)
            if code in {"budget_exceeded", "recheck_budget_exceeded"}:
                return ReverifyResponse(
                    ok=False,
                    error="核对次数已用完",
                    verify=pack_verify(row.get("verify")),
                )
            return ReverifyResponse(ok=False, error=code)
        store.update(review_id, verify=updated)
        after = store.get(review_id) or {}
        after_snap = [
            (i.get("id"), i.get("status"))
            for i in (after.get("items") or [])
            if isinstance(i, dict)
        ]
        if after_snap != status_snap or items_back != (row.get("items") or []):
            # items_back 应是原样快照；档位必须一致
            if after_snap != status_snap:
                logger.error("Design B violation on reverify review_id=%s", review_id)
                raise HTTPException(status_code=500, detail="内部错误：规则档位被意外改动")
    return ReverifyResponse(
        ok=True,
        verify=pack_verify(updated),
        rule_statuses_unchanged=True,
    )
