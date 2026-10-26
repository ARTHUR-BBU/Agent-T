# -*- coding: utf-8 -*-
"""M7 修改建议路由（B7-1）：懒加载按需触发，不在审查主路径。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.services import revision
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
