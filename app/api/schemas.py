"""API request/response schemas."""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


Status = Literal["通过", "需关注", "未找到", "本类不适用"]


class ChecklistItemResult(BaseModel):
    id: str
    name: str
    status: Status
    note: str = ""
    quote: str = ""
    category_na: bool = False


class ReviewSummary(BaseModel):
    id: str
    filename: str
    category: str
    category_label: str
    status: Literal["pending", "processing", "done", "error"] = "pending"
    items: list[ChecklistItemResult] = Field(default_factory=list)
    error: Optional[str] = None
    text_preview: str = ""


class UploadResponse(BaseModel):
    review_id: str
    message: str = "uploaded"


class AskRequest(BaseModel):
    review_id: str
    item_id: str
    question: str


class AskResponse(BaseModel):
    ok: bool
    item_id: str
    item_name: str
    answer: Optional[dict[str, Any]] = None
    raw_text: Optional[str] = None
    error: Optional[str] = None
