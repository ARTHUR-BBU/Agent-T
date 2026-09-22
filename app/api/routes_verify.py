"""A6 有界核验 API（外审批 3：从 routes.py 按领域拆出；URL 与语义不变）。"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from app.api.deps import require_done_row
from app.api.schemas import ConfirmRequest, ConfirmResponse, ReverifyRequest, ReverifyResponse, VerifyBudgetInfo, VerifyInfo
from app.services import verify as verify_service
from app.services.evidence import normalize_verify_state
from app.services.store import store

router = APIRouter()  # 前缀由聚合 router（/api）提供，勿重复

logger = logging.getLogger(__name__)


def pack_verify(raw: dict | None, *, row: dict | None = None) -> VerifyInfo | None:
    """把 store.verify 打成 API 形状（附 budget 快照）。

    PR review P1-a：带 row 上下文时先做读路径归一化——verify 子路由此前
    直读 store 原始行，get_review 归一化过的响应会被 confirm/reverify 的
    未归一化响应刷回旧形态（陈旧 ID + 虚假 verified）。
    """
    if not raw or not isinstance(raw, dict):
        return None
    if row is not None:
        raw = normalize_verify_state(
            raw,
            text=row.get("text") or "",
            document_version=row.get("document_version") or "",
        )
        # 批 2b-②（审计 P1-b）：verify 出口同样做主张标注——done 后写方
        # （trigger/confirm/reverify）的响应此前不带 claim 字段，recheck 后
        # 还可能带陈旧标注；统一在出口对副本重标注（确定性派生）
        from app.services.evidence import annotate_review_claims, normalize_review_evidence
        mini = {
            "text": row.get("text") or "",
            "document_version": row.get("document_version") or "",
            "rule_pack": row.get("rule_pack"),
            "verify": raw,
        }
        raw = annotate_review_claims(normalize_review_evidence(mini))[0]["verify"]
    try:
        info = verify_service.VerifyInfo.model_validate(raw)
    except Exception:  # noqa: BLE001
        return None
    payload = info.model_dump()
    payload["budget"] = info.budget_snapshot()
    return VerifyInfo(**payload)


# routes.py 的向后兼容别名（外部引用零破坏）
_pack_verify = pack_verify


def _merge_evidence_index(review_id: str) -> None:
    """锁内重建 span 索引并持久化（批 2b-①缓存生命周期）。

    必须在 verify_service.lock_for(review_id) 锁内调用——调用方负责。
    重建失败静默跳过（缓存可丢弃、可下次重建，绝不影响主流程）。
    """
    try:
        from app.services.evidence import (
            normalize_review_evidence,
            rebuild_evidence_index,
        )
        fresh = store.get(review_id)
        if not fresh:
            return
        index = rebuild_evidence_index(normalize_review_evidence(fresh))
        store.update(review_id, evidence_index={
            "version": index.get("version", 1),
            "by_span": index.get("by_span") or {},
        })
    except Exception:  # noqa: BLE001
        logger.exception("evidence_index merge failed review_id=%s", review_id)


@router.get("/review/{review_id}/verify", response_model=VerifyInfo)
def get_verify(review_id: str):
    """列出待确认问题 + 剩余预算（A6）。"""
    row = store.get(review_id)
    if not row:
        raise HTTPException(status_code=404, detail="审查记录不存在")
    packed = pack_verify(row.get("verify"), row=row)
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
        # 批 2b-①：done 后写方在锁内重建 span 索引并合并持久化——
        # 「先读→再改→再写」的索引合并全程持锁，Ask/Verify/Objection
        # 并发写不丢条目（审计修订六）
        _merge_evidence_index(review_id)
    packed = pack_verify(out, row=row)
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
        _merge_evidence_index(review_id)
        after = store.get(review_id) or {}
        after_snap = {
            str(i.get("id")): i.get("status")
            for i in (after.get("items") or [])
            if isinstance(i, dict) and i.get("id")
        }
        if after_snap != status_snap:
            logger.error("Design B violation on confirm review_id=%s", review_id)
            raise HTTPException(status_code=500, detail="内部错误：规则档位被意外改动")
    return ConfirmResponse(ok=True, verify=pack_verify(updated, row=row))


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
                    verify=pack_verify(row.get("verify"), row=row),
                )
            return ReverifyResponse(ok=False, error=code)
        store.update(review_id, verify=updated)
        _merge_evidence_index(review_id)
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
        verify=pack_verify(updated, row=row),
        rule_statuses_unchanged=True,
    )
