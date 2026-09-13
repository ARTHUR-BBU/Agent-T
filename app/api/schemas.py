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
    # 外部审计二轮 P1-1：MatchEvidence（规则命中证据坐标）所在条款——
    # 真正触发风险的条款，Ask 上下文第一顺位；旧记录为空
    primary_clause_id: Optional[str] = None


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
    primary_clause_id: Optional[str] = None


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
    # 外部审计二轮：长合同分段阅读覆盖明示 {chunks_total, chunks_reviewed, limited}
    coverage: Optional[dict[str, Any]] = None


class PrecheckInfo(BaseModel):
    """LLM 预审结果（分类≠裁判：永不改档位，仅路由与提示）。"""

    performed: bool = False
    detected_type: str = ""
    confidence: str = "low"
    summary: str = ""
    suspect: bool = False  # low 置信度但倾向与所选不一致 → 非阻断「品类存疑」
    # 阶段 1.3 分支 C：中性立场 + 检出对方视角起草 → 非阻断「立场知情」提示
    stance_notice: bool = False
    # 提示文案由服务端单一来源（stance.counterparty_view_notice）生成下发
    stance_notice_text: str = ""


class QualityObservation(BaseModel):
    """质量层单条观察（阶段 2.1）：铁律 5——带原文引用+待人工确认。"""

    dimension: str  # completeness / consistency / impact
    title: str = ""
    quote: str = ""
    clause_id: Optional[str] = None
    comment: str = ""
    needs_confirm: bool = True  # 代码强制 True（模型无权声明免确认）


class QualityInfo(BaseModel):
    """AI 质量分析（阶段 2.1）：参谋不是裁判——不计分、不改档位。"""

    available: bool = False
    # disabled / no_llm_key / llm_error / parse_failed / budget_exceeded /
    # error / not_attempted（前端对不可用整卡静默隐藏）
    reason: Optional[str] = None
    observations: list[QualityObservation] = Field(default_factory=list)
    disclaimer: str = ""
    dropped_count: int = 0
    coverage: Optional[dict[str, Any]] = None


class ReviewSummary(BaseModel):
    id: str
    filename: str
    category: str
    category_label: str
    status: Literal["pending", "processing", "done", "error"] = "pending"
    # 阶段 0.2：进度段位 triage/scanning/scoring/done/error；旧记录为 None
    # （status 仍是唯一终态权威，前端对 None 退化单行文案）
    stage: Optional[str] = None
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
    # 阶段 1.3：立场声明（元数据；declaration 由服务端单一来源生成，
    # 前端与 docx 只渲染不拼接）
    stance: str = "neutral"
    stance_declaration: str = ""
    # 阶段 2.1 质量层（旧记录为 None；不可用时 available=False 前端静默隐藏）
    quality: Optional[QualityInfo] = None


class UploadResponse(BaseModel):
    """向后兼容：常规路径 review_id 必有值；category_confirm 分支为 None。"""

    review_id: Optional[str] = None
    message: str = "uploaded"
    status: Literal["uploaded", "category_confirm"] = "uploaded"
    precheck: Optional[PrecheckInfo] = None
    suggested_category: Optional[str] = None
    # 阶段 1.3：元素含 stances 嵌套 dict（立场元数据随品类透传）
    supported_categories: list[dict[str, Any]] = Field(default_factory=list)


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
