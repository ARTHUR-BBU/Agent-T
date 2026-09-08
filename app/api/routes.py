"""API routers: upload / review / ask / report."""
from __future__ import annotations

import logging
import threading
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, File, Form, HTTPException, Response, UploadFile

from app.api.schemas import (
    AskRequest,
    AskResponse,
    ReviewSummary,
    ScorecardInfo,
    UploadResponse,
)
from app.graph.pipeline import run_review
from app.services import llm_ask, report as report_service
from app.services.checklist import list_categories
from app.services.store import store

router = APIRouter(prefix="/api")

logger = logging.getLogger(__name__)

# 上传硬限制（外部审计 P1：无限制的大文件可耗尽内存/模型费用）
MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10MB
ALLOWED_SUFFIXES = {".txt", ".md", ".text", ".pdf", ".docx", ".doc"}


@router.get("/categories")
def categories():
    return {"categories": list_categories()}


@router.post("/upload", response_model=UploadResponse)
async def upload(
    file: UploadFile = File(...),
    category: str = Form("procurement"),
):
    # 品类必须显式合法（外部审计：未知品类此前会静默回退采购清单）
    valid_categories = {c["id"] for c in list_categories()}
    if category not in valid_categories:
        raise HTTPException(status_code=422, detail=f"未知合同类型：{category}")

    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="空文件")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="文件超过 10MB 上限，请压缩后上传")
    filename = file.filename or "contract.txt"
    if Path(filename).suffix.lower() not in ALLOWED_SUFFIXES:
        raise HTTPException(
            status_code=400,
            detail="不支持的文件类型（支持 .txt / .md / .pdf / .docx / .doc）",
        )

    rid = store.create(
        filename=filename,
        category=category,
        category_label=category,
        created_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
        status="processing",
        items=[],
        scorecard={},
        blind_candidates=[],
        blind_skipped_messages=[],
        blind_skipped_reason=None,
        blind_enabled=False,
        text="",
        policies=[],
        error=None,
    )

    # 审查放后台线程：上传立即返回 review_id（外部审计 P1：同步等待模型
    # 会拖死请求，前端的 processing 轮询此前形同虚设）
    def _run_review_worker(review_id: str, fname: str, content: bytes, cat: str) -> None:
        try:
            result = run_review(fname, content, category=cat)
            if result.get("error"):
                store.update(
                    review_id,
                    status="error",
                    error=result["error"],
                    text=result.get("text") or "",
                )
            else:
                preview = (result.get("text") or "")[:500]
                store.update(
                    review_id,
                    status="done",
                    items=result.get("items") or [],
                    scorecard=result.get("scorecard") or {},
                    blind_candidates=result.get("blind_candidates") or [],
                    blind_skipped_messages=result.get("blind_skipped_messages") or [],
                    blind_skipped_reason=result.get("blind_skipped_reason"),
                    blind_enabled=bool(result.get("blind_enabled")),
                    text=result.get("text") or "",
                    policies=result.get("policies") or [],
                    category=result.get("category") or cat,
                    category_label=result.get("category_label") or cat,
                    text_preview=preview,
                    error=None,
                )
        except Exception as exc:  # noqa: BLE001
            logger.exception("后台审查失败 review_id=%s", review_id)
            store.update(review_id, status="error", error="审查失败，请重新上传")

    threading.Thread(
        target=_run_review_worker, args=(rid, filename, raw, category), daemon=True
    ).start()

    return UploadResponse(review_id=rid, message="uploaded")


@router.get("/review/{review_id}", response_model=ReviewSummary)
def get_review(review_id: str):
    row = store.get(review_id)
    if not row:
        raise HTTPException(status_code=404, detail="审查记录不存在")
    return ReviewSummary(
        id=row["id"],
        filename=row.get("filename") or "",
        category=row.get("category") or "",
        category_label=row.get("category_label") or "",
        status=row.get("status") or "pending",
        items=row.get("items") or [],
        scorecard=ScorecardInfo(**(row.get("scorecard") or {})),
        blind_candidates=row.get("blind_candidates") or [],
        blind_skipped_messages=row.get("blind_skipped_messages") or [],
        blind_skipped_reason=row.get("blind_skipped_reason"),
        blind_enabled=bool(row.get("blind_enabled")),
        error=row.get("error"),
        text_preview=row.get("text_preview") or "",
        ask_available=bool(llm_ask.get_api_key()),
    )


@router.get("/review/{review_id}/report")
def download_report(review_id: str):
    """M4 导出审查报告（docx）。纯展示层：汇总 store 既有结果，无 LLM 调用。"""
    row = store.get(review_id)
    if not row:
        raise HTTPException(status_code=404, detail="审查记录不存在")
    status = row.get("status") or "pending"
    if status == "error":
        # 失败态不能说成「尚未完成」——掩盖失败会让用户空等（遗留项③，肉饼 P3）
        raise HTTPException(status_code=409, detail="审查失败，请重新上传合同后再导出报告")
    if status != "done":
        raise HTTPException(status_code=409, detail="审查尚未完成，暂不能导出报告")

    try:
        data = report_service.build_report_docx(row)
    except ImportError:
        # python-docx 缺失时不裸抛，给出可操作的错误
        raise HTTPException(status_code=503, detail="服务器未安装 python-docx，无法生成报告")
    except Exception:  # noqa: BLE001
        # 异常详情只进服务端日志，不回给客户端（防泄露路径/实现细节，肉饼审计 P2-1）
        logger.exception("报告生成失败 review_id=%s", review_id)
        raise HTTPException(status_code=500, detail="报告生成失败，请稍后重试")

    base = Path(row.get("filename") or "合同").stem or "合同"
    filename = f"审查报告-{base}-{review_id}.docx"
    headers = {
        # ASCII 兜底 + RFC 5987 中文文件名
        "Content-Disposition": f"attachment; filename=\"report.docx\"; filename*=UTF-8''{quote(filename)}"
    }
    return Response(
        content=data,
        media_type=report_service.DOCX_MEDIA_TYPE,
        headers=headers,
    )


@router.post("/ask", response_model=AskResponse)
def ask(body: AskRequest):
    row = store.get(body.review_id)
    if not row:
        raise HTTPException(status_code=404, detail="审查记录不存在")
    items = row.get("items") or []
    item = next((i for i in items if i.get("id") == body.item_id), None)
    if not item:
        raise HTTPException(status_code=404, detail="清单项不存在")

    result = llm_ask.ask_about_item(
        question=body.question,
        item=item,
        contract_text=row.get("text") or "",
        policies=row.get("policies") or [],
    )
    return AskResponse(
        ok=bool(result.get("ok")),
        item_id=item.get("id", body.item_id),
        item_name=item.get("name", ""),
        answer=result.get("answer"),
        raw_text=result.get("raw_text"),
        error=result.get("error"),
    )
