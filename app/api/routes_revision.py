# -*- coding: utf-8 -*-
"""M7 修改建议路由（B7-1 生成 + B7-2b 选定/连带影响）：懒加载按需触发，
不在审查主路径。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.services import impact, revision
from app.services.store import store

router = APIRouter(prefix="/api", tags=["revision"])


@router.post("/review/{review_id}/items/{item_id}/revision")
def create_revision(review_id: str, item_id: str) -> dict:
    """对已打标 item 生成三版改写草案（用户逐项点按触发）。

    404：审查单或 item 不存在（未检出项拒绝，不扣额度、不调 LLM）；
    422：item 不可改写（未找到/本类不适用/无原文依据）；
    200：{"status": "ok"|"degraded"|"budget_exceeded", ...}——降级与预算
    拒绝均为诚实态，不展示残缺方案，不影响审查结论。"""
    result = revision.generate_revision(store, review_id, item_id)
    if result.get("status") == "not_found":
        raise HTTPException(status_code=404, detail=result.get("reason"))
    if result.get("status") == "not_supported":
        raise HTTPException(status_code=422, detail=result.get("reason"))
    return result


class SelectDraftBody(BaseModel):
    draft_id: str


@router.post("/review/{review_id}/items/{item_id}/revision/select")
def select_revision_draft(review_id: str, item_id: str,
                          body: SelectDraftBody) -> dict:
    """用户选定草案（B7-2b spec §3.1：选定≠生成≠重审三分）。

    404：审查单/item/draft 不存在或归属不符（跨审查单/跨检查项拒绝）；
    422：草案为降级态（无可选定的分析对象）。"""
    result = impact.select_draft(store, review_id, item_id, body.draft_id)
    if result.get("status") == "not_found":
        raise HTTPException(status_code=404, detail=result.get("reason"))
    if result.get("status") == "not_supported":
        raise HTTPException(status_code=422, detail=result.get("reason"))
    return result


@router.post("/review/{review_id}/items/{item_id}/revision/impact")
def analyze_revision_impact(review_id: str, item_id: str,
                            body: SelectDraftBody) -> dict:
    """连带影响分析（B7-2b spec §3.2）：只分析用户选中的那一版草案。

    body.draft_id 必须与当前选定版本一致（选别版=422 draft_mismatch）；
    200：{"status": "ok"|"uncovered"|"degraded"|"budget_exceeded", ...}——
    uncovered=词族未命中相关条款簇的诚实定位结果（非「没有冲突」）。"""
    result = impact.analyze_impact(store, review_id, item_id, body.draft_id)
    if result.get("status") == "not_found":
        raise HTTPException(status_code=404, detail=result.get("reason"))
    if result.get("status") == "not_supported":
        raise HTTPException(status_code=422, detail=result.get("reason"))
    return result
