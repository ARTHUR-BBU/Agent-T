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
    # 阶段 1.1：命中所在条款（clause_index 的 id，如 c05）；未定位到为空
    clause_ids: list[str] = Field(default_factory=list)


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
    clause_ids: list[str] = Field(default_factory=list)


class ClauseInfo(BaseModel):
    """条款索引单条元数据（阶段 1.1）：坐标锚定服务端存档全文，正文不下发。"""

    id: str
    heading: str
    start: int
    end: int
    chars: int = 0


class ClauseIndexInfo(BaseModel):
    strategy: Literal["numbered", "paragraph"] = "paragraph"
    count: int = 0
    clauses: list[ClauseInfo] = Field(default_factory=list)


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


class PrecheckInfo(BaseModel):
    """LLM 预审结果（分类≠裁判：永不改档位，仅路由与提示）。"""

    performed: bool = False
    detected_type: str = ""
    confidence: str = "low"
    summary: str = ""
    suspect: bool = False  # low 置信度但倾向与所选不一致 → 非阻断「品类存疑」


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
    precheck: Optional[PrecheckInfo] = None
    # 阶段 1.1：条款索引元数据（旧记录为 None；不含正文，全文不下发）
    clause_index: Optional[ClauseIndexInfo] = None


class UploadResponse(BaseModel):
    """向后兼容：常规路径 review_id 必有值；category_confirm 分支为 None。"""

    review_id: Optional[str] = None
    message: str = "uploaded"
    status: Literal["uploaded", "category_confirm"] = "uploaded"
    precheck: Optional[PrecheckInfo] = None
    suggested_category: Optional[str] = None
    supported_categories: list[dict[str, str]] = Field(default_factory=list)


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
