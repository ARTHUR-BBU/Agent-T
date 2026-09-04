"""API routers: upload / review / ask."""
from __future__ import annotations

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from app.api.schemas import AskRequest, AskResponse, ReviewSummary, UploadResponse
from app.graph.pipeline import run_review
from app.services import llm_ask
from app.services.checklist import list_categories
from app.services.store import store

router = APIRouter(prefix="/api")


@router.get("/categories")
def categories():
    return {"categories": list_categories()}


@router.post("/upload", response_model=UploadResponse)
async def upload(
    file: UploadFile = File(...),
    category: str = Form("procurement"),
):
    if category not in ("procurement", "nda"):
        # allow unknown but default checklist loader falls back
        pass
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="空文件")
    filename = file.filename or "contract.txt"

    rid = store.create(
        filename=filename,
        category=category,
        category_label=category,
        status="processing",
        items=[],
        text="",
        policies=[],
        error=None,
    )

    try:
        result = run_review(filename, raw, category=category)
        if result.get("error"):
            store.update(
                rid,
                status="error",
                error=result["error"],
                text=result.get("text") or "",
            )
        else:
            preview = (result.get("text") or "")[:500]
            store.update(
                rid,
                status="done",
                items=result.get("items") or [],
                text=result.get("text") or "",
                policies=result.get("policies") or [],
                category=result.get("category") or category,
                category_label=result.get("category_label") or category,
                text_preview=preview,
                error=None,
            )
    except Exception as exc:  # noqa: BLE001
        store.update(rid, status="error", error=str(exc))

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
        error=row.get("error"),
        text_preview=row.get("text_preview") or "",
        ask_available=bool(llm_ask.get_api_key()),
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
