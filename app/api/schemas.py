"""API request/response schemas."""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


Status = Literal["通过", "需关注", "未找到", "本类不适用"]
TagSource = Literal["rule", "blind"]
CompletionStage = Literal["rules_complete", "ai_partial", "quality_complete", "fully_complete"]


class EvidenceRefInfo(BaseModel):
    """证据引用（代码名 EvidenceRef）；界面勿渲染英文学名。"""

    evidence_id: str = ""  # 宪法证据法批：稳定 ID（同发现跨层一致，可引用可去重）
    document_version: str = ""
    quote: str = ""
    start: Optional[int] = None
    end: Optional[int] = None
    clause_id: Optional[str] = None
    verification: str = "unverified"  # verified|unverified|ambiguous|missing
    parse_source: str = "rules"  # rules|blind|quality|ask|fact


class BrokenRefInfo(BaseModel):
    """归一化异常记录（批 2a 审计修订一：改写前捕获，报警器不先擦报警记录）。"""

    where: str  # 容器[下标].evidence，仅调试辅助，不作身份
    old_evidence_id: str
    new_evidence_id: str = ""
    reason: str  # unqualified_id | id_recomputed | downgraded_unlocatable


class EvidenceRegistryInfo(BaseModel):
    """证据登记簿概览（批 2a）：纯读路径派生视图，每次响应即时重建。

    文档作用域账目；occurrence（出现次数）与 unique（唯一张数）分开计数
    ——同票多层各计一次出现，unique 按 evidence_id 去重（空 ID 各计一张）。
    multi_source_unique 只统计现有 ID 的精确一致性，不宣称语义级同证据
    合并（语义级复用是批 2b 服务端 span 规范化）。
    """

    registry_version: int = 1
    rebuilt_at: str = ""
    occurrence_total: int = 0
    unique_total: int = 0
    qualified_occurrence_total: int = 0
    qualified_unique_total: int = 0
    multi_source_unique: int = 0
    broken_ref_count: int = 0
    broken_refs: list[BrokenRefInfo] = Field(default_factory=list)


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
    # 阶段 3 异议层地基（阶段 0 P3 承诺兑现）：命中簇 id + 三分法类别
    # （hardline=永不受理异议 / existence=只收漏报 / heuristic=可收误报；
    # 未标默认 heuristic）。旧记录为 None。
    rule_id: Optional[str] = None
    # 三轮审计 G：校验链拦截非法显式值后，防御路径下原值透传仅供诊断；
    # 消费端（异议层 _eligible_direction）对非法类别永不送审
    rule_class: Optional[Literal["hardline", "existence", "heuristic"]] = None
    evidence: Optional[EvidenceRefInfo] = None


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
    evidence: Optional[EvidenceRefInfo] = None


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
    # 宪法 P0-D2：头尾采样覆盖账目（统一 Coverage schema，规范 23 节）
    coverage: Optional[dict[str, Any]] = None


class QualityObservation(BaseModel):
    """质量层单条观察（阶段 2.1）：铁律 5——带原文引用+待人工确认。"""

    dimension: str  # completeness / consistency / impact
    title: str = ""
    quote: str = ""
    clause_id: Optional[str] = None
    clause_ambiguous: bool = False  # 摘句跨多条款，位置不唯一
    comment: str = ""
    needs_confirm: bool = True  # 代码强制 True（模型无权声明免确认）
    evidence: Optional[EvidenceRefInfo] = None


class FactMaterial(BaseModel):
    """事实材料：可核对事实（主体/金额/日期等）+ 证据引用。"""

    kind: str = ""
    label: str = ""
    value: str = ""
    evidence: Optional[EvidenceRefInfo] = None


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
    facts: list[FactMaterial] = Field(default_factory=list)
    pending_questions: list[str] = Field(default_factory=list)




class ConfirmQuestionInfo(BaseModel):
    """A6 待人工确认问题（有界主动核验）。"""

    id: str
    source: str = ""
    source_ref: str = ""
    question: str = ""
    title: str = ""
    clause_id: Optional[str] = None
    quote: str = ""
    evidence: Optional[EvidenceRefInfo] = None
    verification: str = "unverified"
    fact_ok: Optional[bool] = None
    status: str = "pending"  # pending|confirmed|disputed|rechecked
    human_choice: Optional[str] = None
    human_note: str = ""
    revised_quote: str = ""
    recheck_count: int = 0
    last_recheck: Optional[dict[str, Any]] = None
    triage: str = "must_human"  # must_human|machine_ok|machine_silent
    triage_reason: str = ""
    triage_rule: str = ""


class VerifyBudgetInfo(BaseModel):
    rounds_remaining: int = 0
    clause_fetches_remaining: int = 0
    questions_total: int = 0
    questions_pending: int = 0
    max_questions: int = 8
    max_recheck_per_question: int = 2


class VerifyInfo(BaseModel):
    """A6 有界主动核验状态：疑点→取证→核对→问人→再核。"""

    available: bool = False
    reason: Optional[str] = None
    rounds_used: int = 0
    max_rounds: int = 3
    clause_fetches_used: int = 0
    max_clause_fetches: int = 24
    max_questions: int = 8
    max_recheck_per_question: int = 2
    questions: list[ConfirmQuestionInfo] = Field(default_factory=list)
    disclaimer: str = ""
    document_version: str = ""
    triage_log: list[dict[str, Any]] = Field(default_factory=list)
    budget: Optional[VerifyBudgetInfo] = None


class ConfirmRequest(BaseModel):
    question_id: str
    choice: Literal["confirm", "dispute"]
    human_note: str = ""
    revised_quote: str = ""


class ConfirmResponse(BaseModel):
    ok: bool
    verify: Optional[VerifyInfo] = None
    error: Optional[str] = None


class ReverifyRequest(BaseModel):
    question_id: str


class ReverifyResponse(BaseModel):
    ok: bool
    verify: Optional[VerifyInfo] = None
    error: Optional[str] = None
    # Design B 明示：本响应不含规则档位变更
    rule_statuses_unchanged: bool = True


class Objection(BaseModel):
    """异议候选（阶段 3）：铁律 3——永不改当次档位；采纳产物=规则变更提案。"""

    item_id: str
    rule_id: Optional[str] = None
    rule_class: str = "heuristic"
    direction: str  # false_positive / omission
    quote: str = ""
    counter_evidence: str = ""
    legal_reasoning: str = ""
    stance_check: str = ""
    proposal: str = ""
    accepted: bool = False
    reject_reason: Optional[str] = None
    clause_id: Optional[str] = None
    clause_ambiguous: bool = False
    # 宪法证据法批：受理异议的服务端票据（span 命中位置+版本+parse_source=objection）
    # ——此前 objections 是六层里唯一裸字符串引用的一层（审计 B1-4）
    evidence: Optional[EvidenceRefInfo] = None
    adopted: bool = False
    needs_confirm: bool = True


class ObjectionInfo(BaseModel):
    available: bool = False
    reason: Optional[str] = None
    objections: list[Objection] = Field(default_factory=list)
    rejected_count: int = 0
    disclaimer: str = "异议只是候选线索，不改变逐条核查结论；是否成立由人工与规则修订决定。"
    coverage: Optional[dict[str, Any]] = None  # 外审批 2+三轮审计 D：candidate 维（eligible/sent/reviewed/candidate_limited）+ body 维（body_*/clauses_*/body_limited）


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
    # 阶段 3 异议层（旧记录为 None；不可用整卡静默隐藏）
    objections: Optional[ObjectionInfo] = None
    # 架构 batch3 / A5：规则完成 / AI 部分完成 / 全部完成（旧记录 None）
    completion: Optional[CompletionStage] = None
    document_version: str = ""
    # 顶层事实材料镜像（与 quality.facts 同源；质量关闭时仍可有确定性抽取）
    facts: list[FactMaterial] = Field(default_factory=list)
    # A6 有界主动核验（旧记录 None；前端「需你确认」）
    verify: Optional[VerifyInfo] = None
    # 证据法批 2a：证据登记簿概览（纯读路径派生视图，每次响应即时重建；
    # 旧记录/异常路径为 None，前端零感知）
    evidence_registry: Optional[EvidenceRegistryInfo] = None
    # 导出范围说明（九哥：报告只含本次读到并展示的内容…）
    export_scope_note: str = (
        "报告只含本次读到并展示的内容；未读部分不写入结论"
        "（含规则核查、参考评分与补盲；不含页面 AI 观察及追问）"
    )


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
    quote_verified: Optional[bool] = None
    evidence: Optional[EvidenceRefInfo] = None
