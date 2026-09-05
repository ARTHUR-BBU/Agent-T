"""API request/response schemas."""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


Status = Literal["通过", "需关注", "未找到", "本类不适用"]
TagSource = Literal["rule", "blind"]


class ChecklistItemResult(BaseModel):
    id: str
    name: str
    status: Status
    note: str = ""
    quote: str = ""
    hits: list[str] = Field(default_factory=list)
    category_na: bool = False
    tag_source: Optional[TagSource] = None
    needs_confirm: bool = False


class BlindCandidate(BaseModel):
    id: str
    name: str
    status: Status = "需关注"
    note: str = ""
    quote: str = ""
    hits: list[str] = Field(default_factory=list)
    tag_source: Literal["blind"] = "blind"
    needs_confirm: bool = True
    named_by_scorecard: bool = False


class ScoreSegment(BaseModel):
    key: str
    name: str
    weight: int = 0
    score: int = 0
    comment: str = ""
    na: bool = False


class ScorecardInfo(BaseModel):
    """M3.5 模型评分卡 — advisory only，永不改规则档位。"""

    available: bool = False
    reason: Optional[str] = None
    total: Optional[int] = None
    tier: Optional[dict[str, Any]] = None
    summary: str = ""
    segments: list[ScoreSegment] = Field(default_factory=list)
    caps_applied: list[str] = Field(default_factory=list)
    disclaimer: str = ""
    advisory_only: bool = True
    degraded: bool = False


class ReviewSummary(BaseModel):
    id: str
    filename: str
    category: str
    category_label: str
    status: Literal["pending", "processing", "done", "error"] = "pending"
    items: list[ChecklistItemResult] = Field(default_factory=list)
    scorecard: ScorecardInfo = Field(default_factory=ScorecardInfo)
    blind_candidates: list[BlindCandidate] = Field(default_factory=list)
    blind_skipped_messages: list[str] = Field(default_factory=list)
    blind_skipped_reason: Optional[str] = None
    blind_enabled: bool = False
    error: Optional[str] = None
    text_preview: str = ""
    ask_available: bool = False


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
