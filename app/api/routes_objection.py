"""异议层 API（外审批 3：从 routes.py 按领域拆出；URL 与语义不变）。"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from app.api.deps import require_done_row
from app.api.schemas import ObjectionInfo
from app.services import verify as verify_service
from app.services.store import store

router = APIRouter()  # 前缀由聚合 router（/api）提供，勿重复

logger = logging.getLogger(__name__)


@router.post("/review/{review_id}/objections/adopt", response_model=ObjectionInfo)
def adopt_objection(review_id: str, body: dict):
    """采纳异议为规则改进提案（阶段 3.2）。只标 adopted 键；规则 items 档位
    不动（Design B：快照比对，不一致 500）。提案文本由前端复制给人评审，
    真正的规则变更走版本化修订通道（铁律 3）。"""
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
        # 快照规则档位，写入后对照（Design B）
        status_snap = {
            str(i.get("id")): i.get("status")
            for i in (row.get("items") or [])
            if isinstance(i, dict) and i.get("id")
        }
        obs[index] = {**obs[index], "adopted": True}
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
    return ObjectionInfo(**new_obj)
