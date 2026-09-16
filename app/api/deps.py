"""routes 子模块共享依赖（外审批 3：routes.py 按领域拆分的公共层）。

require_done_row 被 verify 与 objection 两组端点共用；放独立模块避免
子模块 ↔ 聚合 routes.py 的循环 import。语义逐字复刻原 _require_done_row
（404 不存在 / 409 未完成），拆分纯机械，不改任何对外行为。"""
from __future__ import annotations

from fastapi import HTTPException

from app.services.store import store


def require_done_row(review_id: str) -> dict:
    row = store.get(review_id)
    if not row:
        raise HTTPException(status_code=404, detail="审查记录不存在")
    if row.get("status") != "done":
        raise HTTPException(status_code=409, detail="审查尚未完成，暂不能核验确认")
    return row
