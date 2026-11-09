"""API routers: upload / review / ask / report（聚合入口）。

外审批 3 按领域拆分：verify 四端点 → routes_verify.py；异议 adopt →
routes_objection.py；共享依赖 → deps.py。URL 与对外语义零变化（纯机械搬移），
本文件保留核心审查流（categories/upload/review/report/ask）与并发闸门。"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from typing import Any, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile
from fastapi.concurrency import run_in_threadpool

from app.api.schemas import (
    ClaimMigrationWarningInfo,
    EvidenceRegistryInfo,
    ObjectionInfo,
    AskRequest,
    AskResponse,
    ClauseIndexInfo,
    PrecheckInfo,
    QualityInfo,
    RemediateAdoptResponse,
    RemediateContextResponse,
    RemediateEditRequest,
    RemediateRequest,
    RemediateResponse,
    ReviewSummary,
    ScorecardInfo,
    UploadResponse,
)
from app.api.routes_objection import router as _objection_router
from app.api.routes_verify import _merge_evidence_index, _pack_verify, router as _verify_router
from app.api.routes_ai_findings import router as _ai_finding_router
from app.graph.pipeline import run_review
from app.services import ai_findings as ai_findings_service
from app.services import category_suggest as category_suggest_service
from app.services import llm_ask, precheck as precheck_service, report as report_service
from app.services import verify as verify_service
from app.prompts import precheck as precheck_prompts
from app.services import llm_budget, llm_call_log, rate_limit, remediation_budget
from app.services.checklist import list_categories, load_checklist
from app.services.clause_index import build_clause_context
from app.services.extract import ExtractionError, extract_text
from app.services import stance as stance_service
from app.services.store import store

router = APIRouter(prefix="/api")

logger = logging.getLogger(__name__)

# 上传硬限制（外部审计 P1：无限制的大文件可耗尽内存/模型费用）
MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10MB
ALLOWED_SUFFIXES = {".txt", ".md", ".text", ".pdf", ".docx", ".doc"}
# B7-6 附录 B：粘贴文本上限（后端独立校验；超限不截短续审——合同尾部
# 常含免责/争议条款，静默砍尾=丢条款）
_MAX_PASTED_CHARS = 200_000
# B7-6 对话工作台：单合同追问轮次上限（codex 十二轮：50 轮上限在调模型
# 之前检查——超限零费用、无半截记录）
_MAX_ASK_TURNS = 50

# 后台审查并发上限（小智娘门禁 P2-1：无界线程会被脚本刷爆内存与模型费用；
# 槽位占满直接 429，让用户稍后再试而不是排队堆积）
MAX_CONCURRENT_REVIEWS = 4
_review_slots = threading.Semaphore(MAX_CONCURRENT_REVIEWS)

# 解析/预审阶段并发闸门（外部审计批1-②）：extract_text（Docling/pypdf 可能
# 很重）与预审此前发生在 review 槽位**之前**、完全无闸——大文档可在正式
# 审查被限流的同时打爆解析资源。占满降级跳过预审（对齐 precheck busy
# 先例：可用性优先，规则审查不受影响），解析本身同步排队。
MAX_CONCURRENT_PARSES = 4
_parse_slots = threading.Semaphore(MAX_CONCURRENT_PARSES)

# 流式读取块大小：首块读 MAX+1 即可判定超限，不把整个文件读进内存
_READ_CHUNK = 1024 * 1024


async def _read_limited(file: Any) -> tuple[bytes, bool]:
    """分块流式读取上传文件（外部审计批1-②）。

    返回 (已读字节, 是否超限)。超限时立即停止读取——剩余字节不进内存。
    独立成函数便于真锁分块行为（肉饼 PR#23 P3-2：此前测试无法验证
    「读取中途停止」这个资源语义）。
    """
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(_READ_CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_UPLOAD_BYTES:
            return b"".join(chunks), True
        chunks.append(chunk)
    return b"".join(chunks), False




@router.get("/categories")
def categories():
    return {"categories": list_categories()}


_RULE_CATALOG_KEYS = frozenset({
    "id", "name", "description", "segment", "class", "missing_as", "missing_note",
    "na", "na_note", "na_reason", "pass_fulltext_fallback", "signal_gate", "rules",
    "need_attention", "pass", "not_found", "note", "pattern", "patterns", "all_of",
    "any_of", "unless", "unless_window", "matcher", "require_hit_type",
    "hit_alternatives", "start_token", "neg_sensitive", "blank_flag_note",
    "blank_flag_pattern",
})


def _catalog_definition(value):
    """仅公开规则条件白名单，防止未来内部元数据自动暴露。"""
    if isinstance(value, dict):
        return {k: _catalog_definition(v) for k, v in value.items() if k in _RULE_CATALOG_KEYS}
    if isinstance(value, list):
        return [_catalog_definition(v) for v in value]
    return value


@router.get("/rules/catalog/capability")
def rule_catalog_capability():
    return {"available": True}


@router.get("/rules/catalog")
def rule_catalog():
    """只读当前开放品类的清单；不是历史审查的冻结快照。"""
    packs = []
    for category in list_categories():
        cfg = load_checklist(category["id"])
        packs.append({
            "id": category["id"], "label": category["label"],
            "version": cfg.get("version"),
            "items": [{"id": item.get("id"), "name": item.get("name"),
                       "description": item.get("description", ""),
                       "segment": item.get("segment"), "class": item.get("class"),
                       "definition": _catalog_definition(item)}
                      for item in cfg.get("items", [])],
        })
    return {"packs": packs}


# 限频依赖先于 handler 执行：超频请求在读文件/预审之前就被廉价拒绝（阶段 0.5）
def _start_review(
    *,
    filename: str,
    raw: bytes,
    category: str,
    stance: str,
    budget: object | None,
    precheck_record: dict | None,
    parsed_text: str | None = None,
    trace_id: str | None = None,
) -> str:
    """建档 + 启动后台审查线程（外部审计二轮 PR-C 从 upload 抽取）。

    槽位所有权约定：调用方已 acquire _review_slots；本函数任何启动前异常
    直接上抛（调用方负责还槽），线程成功启动后所有权转移给 worker 的
    finally——「一定成对释放」不依赖 worker 是否真的跑起来。
    """
    rid = store.create(
        # B2-3 §2.1 审查单锚点（终审 P1-1）：trace 持久化进审查单——
        # review_id→trace_id→账本全链反查（reviews.data JSON 字段，零迁移）
        trace_id=trace_id,
        filename=filename,
        category=category,
        category_label=category,
        created_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
        status="processing",
        # 阶段 0.2：进度段位（triage→scanning→scoring→done/error）；预审已在
        # 本请求内完成，建档即 triage 完成态，等待页首屏看到的是扫描进行中
        stage="triage",
        # 阶段 1.3：用户声明立场（仅元数据+声明，不进规则引擎）
        stance=stance,
        items=[],
        scorecard={},
        blind_candidates=[],
        blind_skipped_messages=[],
        blind_skipped_reason=None,
        blind_enabled=False,
        quality=None,
        objections=None,
        text="",
        policies=[],
        error=None,
        precheck=precheck_record,
        completion=None,
        document_version="",
        facts=[],
        verify=None,
        export_scope_note=(
            "报告只含本次读到并展示的内容；未读部分不写入结论"
            "（含规则核查、参考评分与补盲；不含页面 AI 观察及追问）"
        ),
    )

    # 审查放后台线程：上传立即返回 review_id（外部审计 P1：同步等待模型
    # 会拖死请求，前端的 processing 轮询此前形同虚设）
    def _run_review_worker(
        review_id: str, fname: str, content: bytes, cat: str, budget: object | None,
        reused_text: str | None, stance: str = "neutral",
        trace_id: str | None = None,
    ) -> None:
        # B2-3 §2.1：daemon 线程 contextvar 不自动继承——worker 顶部显式续证，
        # 管线内 record_node/transport 天然读到同一 trace（全链同号）
        trace_tok = llm_call_log.set_trace(trace_id) if trace_id else None
        # stage 回调：pipeline 节点入口上报 → 逐步落库；update 自身有兜底，
        # 回调失败不影响审查（pipeline 侧还包了一层 try/except）
        def on_stage(stage: str) -> None:
            store.update(review_id, stage=stage)

        def on_partial(completion: str, fields: dict) -> None:
            # A5：阶段结果落盘；规则 items 一经写出即保留（F03 精神）
            # status 仍 processing，直到 fully_complete 才 done——避免半成品可导出误导
            payload = {k: v for k, v in (fields or {}).items() if v is not None}
            payload["completion"] = completion
            store.update(review_id, **payload)

        try:
            try:
                result = run_review(
                    fname, content, category=cat, budget=budget,
                    on_stage=on_stage, on_partial=on_partial,
                    parsed_text=reused_text,
                    stance=stance,
                    review_id=review_id,
                )
                if result.get("error"):
                    store.update(
                        review_id,
                        status="error",
                        stage="error",
                        error=result["error"],
                        text=result.get("text") or "",
                        quality=result.get("quality") or {},
                        objections=result.get("objections") or {},
                        completion=result.get("completion") or None,
                        # 若规则已出，保留 items（不因后续失败抹掉）
                        **(
                            {"items": result["items"]}
                            if result.get("items")
                            else {}
                        ),
                    )
                else:
                    preview = (result.get("text") or "")[:500]
                    store.update(
                        review_id,
                        status="done",
                        stage="done",
                        items=result.get("items") or [],
                        scorecard=result.get("scorecard") or {},
                        blind_candidates=result.get("blind_candidates") or [],
                        blind_skipped_messages=result.get("blind_skipped_messages") or [],
                        blind_skipped_reason=result.get("blind_skipped_reason"),
                        blind_enabled=bool(result.get("blind_enabled")),
                        quality=result.get("quality") or {},
                        objections=result.get("objections") or {},
                        text=result.get("text") or "",
                        policies=result.get("policies") or [],
                        category=result.get("category") or cat,
                        category_label=result.get("category_label") or cat,
                        clause_index=result.get("clause_index"),
                        text_preview=preview,
                        document_version=result.get("document_version") or "",
                        completion=result.get("completion") or "fully_complete",
                        facts=result.get("facts") or [],
                        verify=result.get("verify") or {},
                        evidence_index=result.get("evidence_index"),
                        rule_pack=result.get("rule_pack"),
                        error=None,
                    )
            except Exception:  # noqa: BLE001
                logger.exception("后台审查失败 review_id=%s", review_id)
                store.update(review_id, status="error", stage="error", error="审查失败，请重新上传")
        except Exception:  # noqa: BLE001
            # 兜底（小智娘 P3-3）：update 自身失败（如 DB 锁超时）不得裸抛线程
            logger.exception("后台审查状态写入失败 review_id=%s", review_id)
        finally:
            if trace_tok is not None:
                llm_call_log.reset_trace(trace_tok)
            _review_slots.release()

    threading.Thread(
        target=_run_review_worker,
        args=(rid, filename, raw, category, budget, parsed_text, stance, trace_id),
        daemon=True,
    ).start()
    # B2-3 §2.1：建单后按 trace 回填此前未关联的调用行（幂等，失败仅告警）
    if trace_id:
        llm_call_log.backfill_review_id(rid, trace_id)
    return rid


@router.get("/reviews")
def list_reviews(limit: int = 50):
    """历史审查摘要列表（B7-6 历史侧栏；codex 十二轮 P2-4 口径）：

    本部署共享档案（沿用整站认证同一防线）；TTL 过滤与单条读取同规则；
    只回摘要字段；服务端限条（≤50）。"""
    limit = max(1, min(limit, 50))
    return {"reviews": store.list_reviews(limit=limit)}


@router.post(
    "/upload",
    response_model=UploadResponse,
    dependencies=[Depends(rate_limit.upload_rate_limit)],
)
async def upload(
    file: UploadFile = File(None),
    pasted_text: str = Form(""),
    category: str = Form("procurement"),
    stance: str = Form("neutral"),
    force: bool = Form(False),
):
    # 品类必须显式合法（外部审计：未知品类此前会静默回退采购清单）
    valid_categories = {c["id"] for c in list_categories()}
    if category not in valid_categories:
        raise HTTPException(status_code=422, detail=f"未知合同类型：{category}")
    # 立场校验（阶段 1.3，老钱矩阵）：不在品类可审集合内直接 422——UI 根本
    # 不渲染不可审立场（前馈小字），这里是 API 层兜底；不新增阻断弹窗
    if not stance_service.is_allowed(category, stance):
        raise HTTPException(status_code=422, detail=f"该合同类型不支持立场：{stance}")

    # ---- B7-6 附录 B：粘贴文本模式（codex 十二轮 P1-3/十三轮批示）----
    # 互斥：文件与粘贴文本二选一；超限后端独立校验（不截短续审——
    # 合同尾部常含免责/争议条款，静默砍尾=丢条款）
    has_file = file is not None and bool((file.filename or "").strip())
    has_text = bool(pasted_text.strip())
    if has_file and has_text:
        raise HTTPException(status_code=422, detail="文件与粘贴文本只能二选一")
    if not has_file and not has_text:
        raise HTTPException(status_code=400, detail="请上传文件或粘贴合同文本")
    pasted_body: str | None = None
    if not has_file:
        pasted_body = pasted_text.strip()
        if not pasted_body:
            raise HTTPException(status_code=400, detail="粘贴内容为空")
        if len(pasted_body) > _MAX_PASTED_CHARS:
            raise HTTPException(
                status_code=413,
                detail=f"超出 {_MAX_PASTED_CHARS} 字符上限，请删减后提交",
            )

    # 流式限额（外部审计批1-②）：分块读取，累计超限立即拒收——
    # 旧实现 await file.read() 先把整个文件读进内存才判长度，「10MB 以上
    # 不给审」做到了，「10MB 以上不进内存」没做到
    if has_file:
        filename = file.filename or "contract.txt"
        # 扩展名检查前移（外部审计二轮 PR-C）：零成本的拒绝不该等 body 读完
        if Path(filename).suffix.lower() not in ALLOWED_SUFFIXES:
            raise HTTPException(
                status_code=400,
                detail="不支持的文件类型（支持 .txt / .md / .pdf / .docx / .doc）",
            )
        raw, over_limit = await _read_limited(file)
        if over_limit:
            raise HTTPException(status_code=413, detail="文件超过 10MB 上限，请压缩后上传")
        if not raw:
            raise HTTPException(status_code=400, detail="空文件")
    else:
        # 粘贴模式（附录 B）：文件名占位与文件模式完全同权（无二等标记）
        from datetime import datetime as _dt
        filename = f"粘贴文本_{_dt.now():%Y年%m月%d日}.txt"
        raw = pasted_body.encode("utf-8") if pasted_body else b""

    # 单次审查 LLM 预算（阶段 0.5）：每个 upload 请求一个 Budget 对象，
    # 按引用贯穿预审与审查线程，随 GC 清理；force 重传天然新预算。
    # 创建于预审之前（预审也计入预算），关闭态返回 None 全程直通。
    budget = llm_budget.new_review_budget()
    # B2-3 两项升级专项（§1.3/§2.1）：建议层独立小额度 + 一次上传一张证
    # （trace 单点生成于入口——模块禁止自生成，施工卡 §2.1）
    suggest_budget = llm_budget.new_suggest_budget()
    trace_id = llm_call_log.new_call_id()

    # LLM 预审（spec-llm-precheck）：分类 ≠ 裁判——只决定用哪把尺子/要不要审，
    # 档位仍 100% 出自规则引擎。任何失败降级为照旧开审，绝不阻断主流程。
    # run_in_threadpool（肉饼门禁 P1）：同步调用直接写在 async 路由里
    # 会冻结整个事件循环——解析与 LLM 都挪线程池。
    # 解析阶段并发闸门（外部审计批1-②）：Docling/pypdf 解析很重且此前完全
    # 无闸——大文档可在 review 槽位限流的同时打爆解析资源。非阻塞占位：
    # 占满直接跳过预审（对齐 precheck busy 先例，可用性优先，规则审查不受
    # 影响）；worker 内的 node_parse 本就受 review 槽位（4）约束。
    precheck_record: dict | None = None
    contract_text: str | None = None
    if pasted_body is not None:
        # 粘贴模式免解析（附录 B §3）：纯文本已是解码文本，不占解析槽；
        # 预审分诊/品类建议照常消费 contract_text
        contract_text = pasted_body
    elif not _parse_slots.acquire(blocking=False):
        # 降级必须留痕（肉饼门禁 P2-1，对齐 precheck busy 先例）：闸门满时
        # 跳过预审是合法降级，但静默丢弃会失去品类存疑/立场知情提示且零痕迹
        logger.warning("Parse slots exhausted, skipping precheck (category=%s)", category)
    else:
        try:
            try:
                contract_text = await run_in_threadpool(extract_text, filename, raw)
            except ExtractionError:
                contract_text = None  # 提取失败交给 worker 的 fail-closed 路径统一报错
        finally:
            _parse_slots.release()
    # B2-3 批3：快思考建议层（施工卡 spec-b23-triage-integration）——
    # 外部模型开关默认关（关=零请求零感知）；建议永不切换：与用户选择不一致
    # 一律 category_confirm（置信度只改文案）；other 兜底与 force 重传照常放行
    # （force=用户已在确认弹窗拍板，重传不二次弹）。
    suggest_low_conf: Optional[float] = None  # B2-3 真实卷教训：低置信一致提示
    if (contract_text is not None and category_suggest_service.is_enabled()
            and not force):
        with llm_call_log.record_node(
                "category_suggest",
                prompt_version=category_suggest_service.PROMPT_VERSION,
                trace_id=trace_id,
        ):
            sug_outcome = await run_in_threadpool(
                category_suggest_service.suggest_category, contract_text, category,
                suggest_budget,
            )
        sug = sug_outcome.result
        if (sug_outcome.performed and sug is not None and sug.category
                and sug.category not in {category, "other"}):
            tier = category_suggest_service.confidence_tier(sug.confidence or 0.0)
            supported_ids = {c["id"] for c in list_categories()}
            is_unsupported = sug.category not in supported_ids
            zh = category_suggest_service.category_zh(sug.category)
            if is_unsupported:
                # 老钱签认拒收话术（小智娘 P1-2：unsupported 建议是主场景——
                # 建议层存在的意义恰是抓咨询/劳务派遣，弹窗文案用拒收口径）
                summary = category_suggest_service.reject_notice(sug.category)
            else:
                summary = (f"检测到疑似{zh}（把握较高），与本合约类型不一致，请确认。"
                           if tier == "high" else
                           f"检测到疑似{zh}（把握较低），请确认按哪种类型继续。")
            return UploadResponse(
                message="category_confirm",
                status="category_confirm",
                precheck=PrecheckInfo(
                    performed=True,
                    detected_type=zh,
                    confidence=tier,
                    summary=summary,
                ),
                # P2-1（肉饼门禁）：不支持品类（consulting 等）置 None——
                # 对齐预审 confirm 分支先例，防前端「切换」点击 422
                suggested_category=(sug.category
                                    if sug.category in supported_ids else None),
                supported_categories=list_categories(),
            )
        elif (sug_outcome.performed and sug is not None
                and sug.category == category
                and category_suggest_service.confidence_tier(
                    sug.confidence or 0.0) == "low"):
            # B2-3 真实卷静默盲区教训（final-exam-real-report §2）：建议与用户
            # 一致但把握<0.90——「模型和用户一起错」时现行设计零提示。照常开审，
            # 稍后在报告头挂「品类存疑」非阻断提示（不弹窗/不拦审/不换品类）。
            suggest_low_conf = sug.confidence

    if contract_text is not None:
        # 宪法 P0-D1：precheck 时 review_id 尚未生成（create 在预审后），
        # 账本先记 node 标签——review 关联已由建单后 backfill_review_id 统一补全
        # （B2-3 §2.1）；trace 随本证贯穿
        with llm_call_log.record_node(
                "precheck",
                prompt_version=precheck_prompts.PROMPT_VERSION,
                trace_id=trace_id,
            ):
            outcome = await run_in_threadpool(
                precheck_service.run_precheck, contract_text, category, None, budget, stance
            )
        branch = precheck_service.decide_branch(outcome, category)
        # force（小智娘门禁 P1）：用户在确认弹窗里已拍板（切换或坚持原品类）。
        # 不带 force 重传会重跑预审——LLM 持续不同意时用户永远开不了审（死循环）。
        # force 只跳过 confirm 分支，预审结论仍记录为 suspect 知情提示。
        if branch["action"] != "proceed" and force and outcome.result is not None:
            branch = {"action": "proceed", "suspect": True}
        if branch["action"] != "proceed":
            r = outcome.result
            assert r is not None, "不 proceed 分支必携带预审结果（run_precheck 契约）"
            return UploadResponse(
                message="category_confirm",
                status="category_confirm",
                precheck=PrecheckInfo(
                    performed=True,
                    detected_type=r.detected_type,
                    confidence=r.confidence,
                    summary=r.summary,
                    coverage=outcome.coverage,
                ),
                suggested_category=r.suggested_category if r.is_supported else None,
                supported_categories=list_categories(),
            )
        # 分支 C 知情提示（老钱裁决：中性 + 检出对方视角起草 → 照审 + 非阻断
        # 告知；买方/承租方立场则静默——对方格式合同正是清单靶心场景）
        stance_notice = bool(
            outcome.performed
            and outcome.result is not None
            and stance == "neutral"
            and stance_service.has_counterparty_view_marker(
                category, outcome.result.detected_type
            )
        )
        if outcome.result is not None and (branch["suspect"] or stance_notice):
            r = outcome.result
            precheck_record = {
                "performed": True,
                "detected_type": r.detected_type,
                "confidence": r.confidence,
                "summary": r.summary,
                "suspect": bool(branch["suspect"]),
                "stance_notice": stance_notice,
                "coverage": outcome.coverage,  # 宪法 P0-D2：正常 proceed 路径同账
            }

    # B2-3 静默盲区补丁：低置信一致→报告头「品类存疑」非阻断提示。
    # 不弹窗/不拦审/不换品类；文案服务端单一来源（category_suggest）。
    if suggest_low_conf is not None:
        notice = category_suggest_service.low_conf_agree_notice(suggest_low_conf)
        if precheck_record is None:
            precheck_record = {
                "performed": True,
                "detected_type": category_suggest_service.category_zh(category),
                "confidence": "low",
                "summary": notice,
                "suspect": True,
                "coverage": sug_outcome.coverage,  # 宪法 P0-D2：正常 proceed 路径同账
            }
        else:
            # Codex 勘正（PR #129）：suspect 语义由低置信条件承载——合并时
            # confidence 跟随警示源置 low（否则出现 suspect=True 却
            # confidence=high 的矛盾对象，API 消费方无法识别警示来源）
            precheck_record = {**precheck_record, "suspect": True,
                               "confidence": "low",
                               "summary": f"{precheck_record.get('summary', '')}；{notice}"}

    # 占并发槽位：满则 429（无界线程池被脚本刷 500 次上传 = 500 个 LLM 调用）
    if not _review_slots.acquire(blocking=False):
        raise HTTPException(status_code=429, detail="当前审查排队已满，请稍后再试")

    # 槽位生命周期收口（外部审计二轮 PR-C）：worker 的 finally 只覆盖「线程
    # 已启动」的情形——store.create / Thread.start 在启动前抛异常时槽位无人
    # 归还，4 槽漏光 = 服务永久 429 必须重启。启动期任何异常：还槽再抛。
    try:
        rid = _start_review(
            filename=filename, raw=raw, category=category,
            stance=stance, budget=budget, precheck_record=precheck_record,
            parsed_text=contract_text, trace_id=trace_id,
        )
    except Exception:
        _review_slots.release()
        raise

    return UploadResponse(review_id=rid, message="uploaded")


@router.get("/review/{review_id}", response_model=ReviewSummary)
def get_review(review_id: str):
    row = store.get(review_id)
    if not row:
        raise HTTPException(status_code=404, detail="审查记录不存在")
    pc = row.get("precheck")
    if pc:
        # 分支 C 文案服务端生成（单一来源，前端只渲染不拼接）
        pc = {**pc, "stance_notice_text": (
            stance_service.counterparty_view_notice(row.get("category") or "")
            if pc.get("stance_notice") else ""
        )}
    # 宪法证据法批（第三轮复核 P1-2）：读路径归一化**全部** EvidenceRef
    # 容器 + 批 2b-② 主张标注 + 批 2c 决定一致性检测——统一走唯一权威
    # 流水线（derive_claims_view），页面/verify/adopt/报告四出口同源
    from app.services.evidence import build_evidence_registry, derive_claims_view
    row, warnings, claim_warnings = derive_claims_view(row)
    evidence_registry = build_evidence_registry(row, warnings)
    clause_index = row.get("clause_index")
    stance = row.get("stance") or "neutral"
    quality = row.get("quality")
    objections_row = row.get("objections")
    facts = row.get("facts")
    if not facts and isinstance(quality, dict):
        facts = quality.get("facts") or []
    # 历史草稿可能早于当前禁止清单；读取时也要按现行边界呈现，不能靠按钮拦截。
    from app.services import llm_remediate
    saved_rems = row.get("remediations") or {}
    safe_rems = dict(saved_rems) if isinstance(saved_rems, dict) else {}
    for entry in row.get("items") or []:
        if not isinstance(entry, dict):
            continue
        iid = entry.get("id")
        old = safe_rems.get(iid)
        if (isinstance(old, dict) and old.get("mode") != "guidance"
                and (llm_remediate.is_negative_item(entry)
                     or llm_remediate.needs_specialist(entry, row.get("text") or ""))):
            guidance = llm_remediate.remediate_item(item=entry, contract_text=row.get("text") or "")
            safe_rems[iid] = {"mode": "guidance", "guidance": guidance.get("guidance")}
        elif isinstance(old, dict) and old.get("mode") == "elsewhere":
            # 能力标记只由支持该只读端点的服务器添加，兼容仍在运行的旧预览。
            raw_elsewhere = old.get("elsewhere")
            elsewhere = dict(raw_elsewhere) if isinstance(raw_elsewhere, dict) else {}
            elsewhere["context_available"] = True
            safe_rems[iid] = {**old, "elsewhere": elsewhere}
    return ReviewSummary(
        precheck=PrecheckInfo(**pc) if pc else None,
        id=row["id"],
        filename=row.get("filename") or "",
        category=row.get("category") or "",
        category_label=row.get("category_label") or "",
        status=row.get("status") or "pending",
        stage=row.get("stage"),
        items=row.get("items") or [],
        stance=stance,
        stance_declaration=stance_service.declaration(row.get("category") or "", stance),
        scorecard=ScorecardInfo(**(row.get("scorecard") or {})),
        clause_index=ClauseIndexInfo(**clause_index) if clause_index else None,
        blind_candidates=row.get("blind_candidates") or [],
        blind_skipped_messages=row.get("blind_skipped_messages") or [],
        blind_skipped_reason=row.get("blind_skipped_reason"),
        blind_enabled=bool(row.get("blind_enabled")),
        quality=QualityInfo(**quality) if quality else None,
        objections=ObjectionInfo(**objections_row) if objections_row else None,
        error=row.get("error"),
        text_preview=row.get("text_preview") or "",
        ask_available=bool(llm_ask.get_api_key()),
        completion=row.get("completion"),
        document_version=row.get("document_version") or "",
        # 2b-③（§3.6）：追问涉及证据计数（只计合格票，§10 钉 4）——计数可见，
        # 明细与问题内容永不下发（§4.9 规则三）
        ask_evidence_count=llm_ask.count_valid_ask_evidence(row),
        facts=facts or [],
        evidence_registry=EvidenceRegistryInfo(**evidence_registry),
        claim_migration_warnings=[ClaimMigrationWarningInfo(**w) for w in claim_warnings],
        verify=_pack_verify(row.get("verify"), row=row),
        # B7-5a：统一「AI 发现」清单（观察+疑点合并视图；纯读派生，不写回）
        ai_findings=ai_findings_service.review_findings(row),
        # B7-6：共享对话流（持久化随单；无对话为 None）
        ask_thread=row.get("ask_thread"),
        # B7-6c：补写建议随单（草稿卡刷新恢复）
        remediations=safe_rems or None,
        export_scope_note=row.get("export_scope_note")
        or (
            "报告只含本次读到并展示的内容；未读部分不写入结论"
            "（含规则核查、AI 发现与确认记录"
            + (
                "、问清楚对话记录"
                if (row.get("ask_thread") or {}).get("turns")
                else ""
            )
            + ("、补充参考及选用记录" if row.get("remediations") else "")
            + "、参考评分与补盲）"
        ),
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
        # 2c（§4.0，审计终审 P1）：报告入口走统一派生流水线——原件直进会让
        # 主张编号/证据状态/决定一致性全部缺位（页面盖了红章、打印版还是旧的）
        from app.services.evidence import derive_claims_view
        view, warnings, _claim_warnings = derive_claims_view(row)
        # B7-3b 草案章节数据（§五A）：纯读库组装，零 LLM 零扣额；全卷无
        # 修订活动返回空列表 → 章节整体不渲染，报告字节契约不变
        from app.services import impact as impact_service
        view["revision_section"] = impact_service.revision_report_section(
            store, review_id)
        # B7-5a：观察类发现（无问题行、无主张编号）的确认/争议记录——
        # 派生问题类走 verify 决定链自动入节，这里只补观察类缺口
        obs_findings = ai_findings_service.review_findings(view).get("findings") or []
        view["ai_finding_records"] = [
            {"title": f.get("title") or "", "status": f.get("status") or ""}
            for f in obs_findings
            if not f.get("question_id") and f.get("status") in ("confirmed", "disputed")
        ]
        # B7-5a（codex 三轮 P2-5）：观点类发现正文导出（「AI 观点」小节）
        _dim_labels = {
            "completeness": "有没有漏写",
            "consistency": "有没有自相矛盾",
            "impact": "对您的影响",
        }
        view["ai_finding_opinions"] = [
            {
                "title": f.get("title") or "",
                "comment": f.get("comment") or "",
                "dimension_label": _dim_labels.get(
                    f.get("dimension") or "", "观点"),
            }
            for f in obs_findings if f.get("fact_type") is False
        ]
        # B7-6c（老钱 2.3）：补写建议留痕——草稿全文+选用状态进报告
        _rems = row.get("remediations") or {}
        view["remediation_records"] = [
            {
                "item_id": iid,
                "item_name": next(
                    (it.get("name") or "" for it in (row.get("items") or [])
                     if isinstance(it, dict) and it.get("id") == iid), ""),
                **(r if isinstance(r, dict) else {}),
            }
            for iid, r in (_rems.items() if isinstance(_rems, dict) else [])
        ]
        # B7-6（codex 十五轮 P1-1）：问清楚对话记录入报告——纯读库组装，
        # 无对话返回空列表 → 章节整体不渲染（旧报告字节契约不变）
        _thread = row.get("ask_thread") or {}
        _t_list = _thread.get("turns") if isinstance(_thread, dict) else None
        view["ask_thread_section"] = [
            {
                "item_name": t.get("item_name") or "",
                "question": t.get("question") or "",
                "state": t.get("state") or "",
                "error": t.get("error"),
                "answer": t.get("answer") if isinstance(t.get("answer"), dict) else None,
                "source": t.get("source"),
            }
            for t in (_t_list or []) if isinstance(t, dict)
        ]
        data = report_service.build_report_docx(view, warnings)
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


def _record_ask_evidence(
    *,
    review_id: str,
    row: dict,
    item: dict,
    body: AskRequest,
    result: dict,
) -> None:
    """Ask 引用入库（2b-③ §3.4）：锁内「读→追加→写回→重建索引」四步全程
    同一把锁（与 verify 三端点同锁，并发写互不覆盖）。入库失败静默跳过——
    账本是观测设施，绝不影响 Ask 主流程（§3.3 原话）。
    """
    try:
        _record_ask_evidence_locked(review_id=review_id, row=row, item=item, body=body, result=result)
    except Exception:  # noqa: BLE001
        # 账本是观测设施（§3.3）：入库任何失败（SQLite 瞬时错误/账本数据
        # 异常等）只留日志——已成功且已付费的 Ask 绝不因可选路径变 500
        logger.exception("ask evidence ledger write failed review_id=%s", review_id)


def _record_ask_evidence_locked(
    *,
    review_id: str,
    row: dict,
    item: dict,
    body: AskRequest,
    result: dict,
) -> None:
    # §3.5 规则四：TTL=0（永不过期）时不登记——宁缺毋滥
    if store.ttl_seconds <= 0:
        return
    # §3.5：row 上限（MAX_ASK_EVIDENCE_ENTRIES 启动期已严格校验）；超限后
    # 新追问照常回答、不再入账、不计错误
    existing = row.get("ask_evidence") or []
    if len(existing) >= llm_ask.MAX_ASK_EVIDENCE_ENTRIES:
        return
    entry = llm_ask.build_ask_evidence_entry(
        question=body.question,
        item_id=str(item.get("id") or body.item_id),
        result=result,
        row_document_version=row.get("document_version") or "",
        row_text=row.get("text") or "",
    )
    if entry is None:  # §3.3 门禁四条任一不过：不入账，响应不变
        return
    with verify_service.lock_for(review_id):
        # 锁内以 fresh 行重验归属（§10 钉 3 对抗面：A 合同票据不能放进 B
        # 合同行——并发期间行被替换/改版时，按陈旧快照写入就是跨合同混账）
        fresh = store.get(review_id)
        if fresh is None:
            return
        recheck = llm_ask.build_ask_evidence_entry(
            question=body.question,
            item_id=str(item.get("id") or body.item_id),
            result=result,
            row_document_version=fresh.get("document_version") or "",
            row_text=fresh.get("text") or "",
        )
        if recheck is None:
            return
        entries = fresh.get("ask_evidence") or []
        if len(entries) >= llm_ask.MAX_ASK_EVIDENCE_ENTRIES:
            return
        entries = list(entries) + [recheck]
        store.update(review_id, ask_evidence=entries)
        _merge_evidence_index(review_id)


@router.post(
    "/ask",
    response_model=AskResponse,
    dependencies=[Depends(rate_limit.ask_rate_limit)],
)
def ask(body: AskRequest):
    row = store.get(body.review_id)
    if not row:
        raise HTTPException(status_code=404, detail="审查记录不存在")
    items = row.get("items") or []
    item = next((i for i in items if i.get("id") == body.item_id), None)
    # 未物化观点只开放解释追问；始终从当前审查单重建，不能由客户端提供观点正文。
    if item is None and body.item_id.startswith("opinion:"):
        from app.services.evidence import derive_claims_view
        normalized, _, _ = derive_claims_view(row)
        key = body.item_id[len("opinion:"):]
        finding = next((f for f in ai_findings_service.review_findings(normalized).get("findings", [])
                        if f.get("finding_key") == key and not f.get("fact_type")), None)
        if finding and finding.get("status") != "disputed":
            item = {"id": body.item_id, "name": finding.get("title") or "AI 观点",
                    "status": "AI 观点", "tag_source": "ai_opinion",
                    "note": finding.get("comment") or "", "quote": finding.get("quote") or "",
                    "clause_ids": [finding["clause_id"]] if finding.get("clause_id") else []}
    if not item:
        raise HTTPException(status_code=404, detail="清单项不存在")
    # B7-5a 反悔闭环：停用的 AI 发现不可追问（与生成/选定/牵连同一资格判据）
    if item.get("tag_source") == "ai_finding" and item.get("status") != "需关注":
        raise HTTPException(status_code=409,
                            detail="该 AI 发现已争议撤回，暂不能追问（可恢复确认后再操作）")
    # B7-6c 窄开（老钱裁定）：未找到条目只在补写参考生成后才开放讨论——
    # 前后端两道闸（前端 askable 同口径），防止未找到变成无约束问答口
    _remediated = False
    _rem: dict = {}
    if item.get("status") == "未找到":
        from app.services import llm_remediate
        if (llm_remediate.is_negative_item(item)
                or llm_remediate.needs_specialist(item, row.get("text") or "")):
            raise HTTPException(status_code=409, detail="该条目需由专业人士核对，不能通过补写草稿追问")
        _rems = row.get("remediations") or {}
        _candidate = _rems.get(body.item_id) if isinstance(_rems, dict) else None
        _rem = _candidate if isinstance(_candidate, dict) else {}
        _remediated = _rem.get("mode") == "draft"
        if not _remediated:
            raise HTTPException(
                status_code=409,
                detail="「未找到」条目请先点「看看怎么补」生成补充参考，再继续讨论")

    # ---- B7-6 对话工作台：turn 生命周期（codex 十二/十三轮规则）----
    # 幂等键=客户端 request_id；服务端唯一键 (review_id, request_id)；
    # 额度检查+turn 预留锁内完成；等模型期间释放锁；归档按 turn（谁的归谁）
    request_id = (body.request_id or "").strip() or uuid.uuid4().hex
    # B7-6（codex 十五轮 P2-6）：编号与（条目,问题）绑定——同编号换问题
    # 会造成「模型答新问题、档案存旧问题」的归档错配，直接拒绝
    _q_norm = (body.question or "")[:llm_ask.MAX_QUESTION_CHARS]
    with verify_service.lock_for(body.review_id):
        fresh = store.get(body.review_id) or row
        thread = fresh.get("ask_thread") or {"turns": []}
        if not isinstance(thread.get("turns"), list):
            thread = {"turns": []}
        existing = next(
            (t for t in thread["turns"]
             if isinstance(t, dict) and t.get("request_id") == request_id), None)
        if existing is not None and (
            existing.get("item_id") != (item.get("id") or body.item_id)
            or existing.get("question") != _q_norm
        ):
            return AskResponse(
                ok=False, item_id=item.get("id", body.item_id),
                item_name=item.get("name", ""),
                error="该请求编号已绑定其他提问，请修改问题后重新发送")
        turn_no: int
        if existing is not None and existing.get("state") == "processing":
            # 同 request_id 再到且在途：返回状态，不再调用模型
            return AskResponse(
                ok=True, item_id=existing.get("item_id") or body.item_id,
                item_name=existing.get("item_name") or item.get("name", ""),
                deduped=True, turn_no=existing.get("turn_no"))
        if existing is not None and existing.get("state") == "success":
            # 已成功：直接返回存档答案（首次响应丢失重发不二次调用/不二次计费）
            return AskResponse(
                ok=True, item_id=existing.get("item_id") or body.item_id,
                item_name=existing.get("item_name") or item.get("name", ""),
                answer=existing.get("answer"),
                raw_text=existing.get("raw_text"),
                error=existing.get("error"),
                quote_verified=existing.get("quote_verified"),
                deduped=True, turn_no=existing.get("turn_no"))
        if existing is not None:
            # 失败轮重试：原 turn 复活流转（processing→…→success），不新开
            existing["state"] = "processing"
            existing["created_at"] = time.time()
            store.update(body.review_id, ask_thread=thread)
            turn_no = int(existing.get("turn_no") or 0)
        else:
            # 新问题：50 轮上限在调模型之前检查（超限零费用、无半截记录）
            turns = thread["turns"]
            if len(turns) >= _MAX_ASK_TURNS:
                return AskResponse(
                    ok=False, item_id=item.get("id", body.item_id),
                    item_name=item.get("name", ""),
                    error=f"对话已达 {_MAX_ASK_TURNS} 轮上限，本次审查的追问到此为止")
            turn_no = (turns[-1].get("turn_no") or 0) + 1 if turns else 1
            turns.append({
                "turn_no": turn_no,
                "request_id": request_id,
                "item_id": item.get("id") or body.item_id,
                "item_name": item.get("name") or "",
                "state": "processing",
                "question": (body.question or "")[:llm_ask.MAX_QUESTION_CHARS],
                "created_at": time.time(),
                # B7-6c：补写讨论轮来源标记（线程/报告「· 补写」标签）
                **({"source": "remediate"}
                   if item.get("status") == "未找到" else {}),
                **({"source": "opinion"} if item.get("tag_source") == "ai_opinion" else {}),
            })
            store.update(body.review_id, ask_thread=thread)

    # ---- 模型调用（锁外；codex 十三轮：等模型期间释放锁）----
    # B7-6（codex 十五轮 P1-2，计划 §三-补2）：最近 10 轮成功问答作前情
    # 附给模型（跨条目追问能看到前情）——纯读库组装，上限在 llm_ask 内裁
    history_turns: list[dict] = []
    try:
        fresh_h = store.get(body.review_id) or row
        h_thread = fresh_h.get("ask_thread") or {}
        h_turns = h_thread.get("turns") if isinstance(h_thread, dict) else None
        if isinstance(h_turns, list):
            for t in h_turns:
                if (isinstance(t, dict) and t.get("state") == "success"
                        and t.get("request_id") != request_id
                        and isinstance(t.get("answer"), dict)):
                    history_turns.append({
                        "item_name": t.get("item_name") or "",
                        "question": t.get("question") or "",
                        "answer": t["answer"],
                    })
            history_turns = history_turns[-10:]
    except Exception:  # noqa: BLE001 — 前情是增益项，读库异常不拦主流程
        logger.exception("读取对话前情失败 review_id=%s", body.review_id)
        history_turns = []
    # 宪法 P0-D1/D5：Ask 也进 LLM 账本（不能成为观测盲区）
    # B2-3 §2.1：ask 是独立入口——每次追问生成一张自己的证（关联 review 既有值）
    with llm_call_log.record_node(
        "ask", body.review_id,
        prompt_version=(llm_ask.OPINION_PROMPT_VERSION if item.get("tag_source") == "ai_opinion"
                        else llm_ask.REMEDIATION_DISCUSSION_PROMPT_VERSION if _remediated
                        else llm_ask.PROMPT_VERSION),
        trace_id=llm_call_log.new_call_id()):
            result = llm_ask.ask_about_item(
            question=body.question,
            item=item,
            contract_text=row.get("text") or "",
            policies=row.get("policies") or [],
            # 条款上下文（外部审计批1-③）：命中条款全文+相邻条款做主上下文；
            # 无索引/未定位回退头尾采样（llm_ask 内处理）
            clause_context=build_clause_context(
            row.get("text") or "",
            row.get("clause_index") or {},
            item.get("clause_ids") or [],
            primary_clause_id=item.get("primary_clause_id") or "",
        ),
            # A4：立场进追问解释（规则档位不变）
            category=row.get("category") or "procurement",
            stance=row.get("stance") or "neutral",
            history=history_turns,
            skip_status_gate=_remediated,
            remediation_draft=(_rem.get("draft") or {}) if _remediated else None,
        )
    ok_flag = bool(result.get("ok"))
    _archive_ask_turn(body.review_id, request_id, turn_no, result, ok_flag)
    _record_ask_evidence(review_id=body.review_id, row=row, item=item, body=body, result=result)
    return AskResponse(
        ok=ok_flag,
        item_id=item.get("id", body.item_id),
        item_name=item.get("name", ""),
        answer=result.get("answer"),
        raw_text=result.get("raw_text"),
        error=result.get("error"),
        quote_verified=result.get("quote_verified"),
        evidence=result.get("evidence"),
        turn_no=turn_no,
    )


@router.post(
    "/review/{review_id}/items/{item_id}/remediate",
    response_model=RemediateResponse,
    dependencies=[Depends(rate_limit.ask_rate_limit)],
)
def remediate_item(review_id: str, item_id: str,
                   _body: RemediateRequest = RemediateRequest()):
    """B7-6c「看看怎么补」：先查写在别处/替代安排，确缺失出中性示范草稿。

    老钱裁定落地：负清单类目（主体/签署/格式条款公平性）入口级拦截——
    零模型零费用，只给行动指引；草稿不改档位（附件工件，铁律）。"""
    row = store.get(review_id)
    if not row:
        raise HTTPException(status_code=404, detail="审查记录不存在")
    if (row.get("status") or "") != "done":
        raise HTTPException(status_code=409, detail="审查尚未完成，暂不能生成补充参考")
    items = row.get("items") or []
    item = next((i for i in items if i.get("id") == item_id), None)
    if not item:
        raise HTTPException(status_code=404, detail="清单项不存在")
    if item.get("status") != "未找到":
        raise HTTPException(status_code=409, detail="仅「未找到」条目提供补写参考")
    if item.get("tag_source") == "blind":
        raise HTTPException(status_code=409, detail="补盲候选不提供补写参考")

    from app.services import llm_remediate
    no_llm = (llm_remediate.is_negative_item(item)
              or llm_remediate.needs_specialist(item, row.get("text") or ""))
    cached = (row.get("remediations") or {}).get(item_id)
    if (isinstance(cached, dict) and cached.get("mode") in ("guidance", "elsewhere", "draft")
            and (not no_llm or cached.get("mode") == "guidance")):
        return RemediateResponse(ok=True, item_id=item_id,
                                 item_name=item.get("name") or "", **cached)
    if no_llm:
        return _execute_remediation(review_id, item_id, row, item, no_llm=True, guard=None)
    with remediation_budget.item_lock(review_id, item_id) as guard:
        if guard is None:
            raise HTTPException(status_code=409, detail="该条补写正在处理或系统繁忙，请稍后刷新查看")
        return _execute_remediation(review_id, item_id, row, item, no_llm=False, guard=guard)


def _execute_remediation(review_id: str, item_id: str, row: dict[str, Any], item: dict[str, Any],
                         *, no_llm: bool, guard: remediation_budget.CallGuard | None):
    from app.services import llm_remediate
    if no_llm:
        # 负清单：入口级拦截（不进模型，零费用）——直接给行动指引
        result = llm_remediate.remediate_item(
            item=item, contract_text=row.get("text") or "",
            category=row.get("category") or "procurement")
    else:
        if guard is None:
            raise RuntimeError("补写任务缺少锁凭证")
        # 按最坏路径预留全部分段调用，避免处理一半时额度不足。
        if not str(row.get("text") or "").strip():
            return RemediateResponse(ok=False, item_id=item_id,
                                     item_name=item.get("name") or "", mode="error",
                                     error="合同正文不可用，无法核查补充参考")
        required_calls = llm_remediate.calls_needed(row.get("text") or "")
        if required_calls is None:
            return RemediateResponse(ok=False, item_id=item_id,
                                     item_name=item.get("name") or "", mode="error",
                                     error="合同超过补写查证的长度上限，请人工核对")
        if not (llm_ask._deepseek_key() or llm_ask._zhipu_key() or llm_ask._xai_key()):
            return RemediateResponse(ok=False, item_id=item_id,
                                     item_name=item.get("name") or "", mode="error",
                                     error="补写建议暂未开通")
        reservation = remediation_budget.reserve(review_id, item_id, calls=required_calls, guard=guard)
        if reservation != "reserved":
            if reservation == "expired":
                raise HTTPException(status_code=404, detail="审查记录已过期")
            if reservation == "interrupted":
                raise HTTPException(
                    status_code=409,
                    detail="上次补写已中断，已发出的模型请求可能已计费；如需重新检查，请点击重试。")
            if reservation == "legacy_processing":
                raise HTTPException(
                    status_code=409,
                    detail="旧版任务状态无法安全确认，请重新提交合同审查，避免重复调用。")
            if reservation == "exhausted":
                llm_call_log.emit_skip(
                    node="remediate", provider="not_called", model="not_called",
                    review_id=review_id, prompt_version=llm_remediate.PROMPT_VERSION,
                    outcome="budget_exceeded",
                    error_detail=(f"remediation budget exhausted (attempts={remediation_budget.MAX_ATTEMPTS_PER_REVIEW}, "
                                  f"calls={remediation_budget.MAX_CALLS_PER_REVIEW}, requested={required_calls})"))
            if reservation == "success":
                fresh = store.get(review_id) or {}
                entry = (fresh.get("remediations") or {}).get(item_id)
                if isinstance(entry, dict):
                    return RemediateResponse(ok=True, item_id=item_id,
                                             item_name=item.get("name") or "", **entry)
            message = ("该条补写正在处理，请稍后刷新查看" if reservation == "processing"
                       else "本次审查的补写调用额度已用完或结果不可用，请重新提交合同审查")
            raise HTTPException(status_code=409, detail=message)
        try:
            with llm_call_log.record_node(
                "remediate", review_id,
                prompt_version=llm_remediate.PROMPT_VERSION,
                trace_id=llm_call_log.new_call_id()):
                result = llm_remediate.remediate_item(
                    item=item, contract_text=row.get("text") or "",
                    clause_context=build_clause_context(
                        row.get("text") or "",
                        row.get("clause_index") or {},
                        item.get("clause_ids") or [],
                        primary_clause_id=item.get("primary_clause_id") or "",
                    ),
                    category=row.get("category") or "procurement",
                    before_call=lambda: remediation_budget.note_call_started(guard),
                )
        except Exception:
            remediation_budget.finish(review_id, item_id, success=False)
            raise

    # 随单持久（TTL 同审查单；刷新恢复草稿卡；选用状态在此流转）
    rem_entry = {
        "mode": result.get("mode") or "error",
        "elsewhere": result.get("elsewhere") or {},
        "draft": result.get("draft") or {},
        "guidance": result.get("guidance"),
        "quote_verified": result.get("quote_verified"),
        "adopted": False,
        "created_at": time.time(),
    }
    if result.get("mode") in ("guidance", "elsewhere", "draft"):
        def save_entry(fresh: dict[str, Any]) -> None:
            live_item = next((i for i in fresh.get("items") or []
                              if isinstance(i, dict) and i.get("id") == item_id), None)
            if not live_item or live_item.get("status") != "未找到":
                raise HTTPException(status_code=409, detail="条目状态已改变，请刷新后重试")
            rems = dict(fresh.get("remediations") or {})
            # 不覆盖已保存的结果或用户选用状态。
            if no_llm and isinstance(rems.get(item_id), dict) and rems[item_id].get("mode") != "guidance":
                # 保留旧工件供审计，不再作为当前可选用草稿展示。
                rem_entry["previous_record"] = rems[item_id]
                rems[item_id] = rem_entry
            else:
                rems.setdefault(item_id, rem_entry)
            fresh["remediations"] = rems

        try:
            updated = store.mutate(review_id, save_entry)
        except Exception:
            if not no_llm:
                remediation_budget.finish(review_id, item_id, success=False)
            raise
        if updated is None:
            if not no_llm:
                remediation_budget.finish(review_id, item_id, success=False)
            raise HTTPException(status_code=404, detail="审查记录已过期")

    if not no_llm:
        remediation_budget.finish(review_id, item_id,
                                  success=result.get("mode") in ("elsewhere", "draft"))

    if result.get("mode") == "elsewhere" and isinstance(result.get("elsewhere"), dict):
        result["elsewhere"]["context_available"] = True
    return RemediateResponse(
        ok=bool(result.get("ok")),
        item_id=item_id,
        item_name=item.get("name") or "",
        mode=result.get("mode") or "error",
        guidance=result.get("guidance"),
        elsewhere=result.get("elsewhere") if result.get("ok") else None,
        draft=result.get("draft") if result.get("ok") else None,
        quote_verified=result.get("quote_verified"),
        error=result.get("error"),
    )


@router.get(
    "/review/{review_id}/items/{item_id}/remediate/context/{quote_index}",
    response_model=RemediateContextResponse,
)
def get_remediation_context(review_id: str, item_id: str, quote_index: int):
    """仅返回已保存摘句周围的有限原文；多处命中时不猜测位置。"""
    row = store.get(review_id)
    if not row:
        raise HTTPException(status_code=404, detail="审查记录不存在")
    if row.get("status") != "done":
        raise HTTPException(status_code=409, detail="审查尚未完成")
    item = next((i for i in row.get("items") or []
                 if isinstance(i, dict) and i.get("id") == item_id), None)
    from app.services import llm_remediate
    if (not item or item.get("status") != "未找到"
            or llm_remediate.is_negative_item(item)
            or llm_remediate.needs_specialist(item, row.get("text") or "")):
        raise HTTPException(status_code=404, detail="该条目没有可查看的补写查证原文")
    rems = row.get("remediations") or {}
    rem = rems.get(item_id) if isinstance(rems, dict) else None
    elsewhere = rem.get("elsewhere") if isinstance(rem, dict) and rem.get("mode") == "elsewhere" else None
    quotes = elsewhere.get("quotes") if isinstance(elsewhere, dict) else None
    if not isinstance(quotes, list) or not 0 <= quote_index < min(len(quotes), 3):
        raise HTTPException(status_code=404, detail="原文摘句不存在")
    quote = quotes[quote_index]
    if not isinstance(quote, str) or not quote.strip():
        raise HTTPException(status_code=404, detail="原文摘句不存在")
    from app.services.evidence import locate_quote_span
    text = str(row.get("text") or "")
    start, end, status = locate_quote_span(text, quote)
    if status != "verified" or start is None or end is None:
        return RemediateContextResponse(status="ambiguous" if status == "ambiguous" else "missing")
    heading = ""
    index = row.get("clause_index") or {}
    clauses = index.get("clauses") if isinstance(index, dict) else []
    for clause in clauses or []:
        if not isinstance(clause, dict):
            continue
        left, right = clause.get("start"), clause.get("end")
        if isinstance(left, int) and isinstance(right, int) and 0 <= left <= start < end <= right <= len(text):
            candidate = str(clause.get("heading") or "")[:200]
            needle = "".join(candidate.rstrip("…").split())
            if needle and needle in "".join(text[left:min(right, left + 400)].split()):
                heading = candidate
            break
    match_end = min(end, start + 1200)
    before_start = max(0, start - 500)
    after_end = min(len(text), end + 500)
    return RemediateContextResponse(
        status="verified", heading=heading, before=text[before_start:start],
        match=text[start:match_end], after=text[end:after_end] if match_end == end else "",
        truncated=before_start > 0 or after_end < len(text) or match_end < end,
    )


@router.put(
    "/review/{review_id}/items/{item_id}/remediate",
    response_model=RemediateResponse,
)
def edit_remediation(review_id: str, item_id: str, body: RemediateEditRequest):
    """仅保存用户自己修改的示范文本，不调用模型、不变更清单档位。"""
    from app.services import llm_remediate
    updated_text = body.clause_draft.strip()
    if not updated_text:
        raise HTTPException(status_code=422, detail="补充参考不能为空")

    def edit(fresh: dict[str, Any]) -> None:
        item = next((i for i in fresh.get("items") or []
                     if isinstance(i, dict) and i.get("id") == item_id), None)
        if (not item or item.get("status") != "未找到"
                or llm_remediate.is_negative_item(item)
                or llm_remediate.needs_specialist(item, fresh.get("text") or "")):
            raise HTTPException(status_code=409, detail="该条目不能调整补写草稿")
        rems = fresh.get("remediations") or {}
        rem = rems.get(item_id) if isinstance(rems, dict) else None
        if not isinstance(rem, dict) or rem.get("mode") != "draft":
            raise HTTPException(status_code=409, detail="该条目暂无补写草稿")
        draft = rem.get("draft") or {}
        if not isinstance(draft, dict) or draft.get("clause_draft") != body.expected_draft:
            raise HTTPException(status_code=409, detail="草稿已在其他地方更改，请刷新后再调整")
        if rem.get("adopted") and updated_text != body.expected_draft:
            history = rem.setdefault("past_adoptions", [])
            if len(history) >= 10:
                raise HTTPException(status_code=409, detail="历史选用记录已达上限，请重新审查合同")
            history.append({"clause_draft": body.expected_draft, "adopted_at": rem.get("adopted_at")})
            rem["adopted"] = False
            rem.pop("adopted_at", None)
        rem.setdefault("initial_draft", dict(draft))
        draft["clause_draft"] = updated_text
        # 原先由 AI 提供的留空与关联提示可能已不适用于用户修改的文本。
        draft["blanks"] = []
        draft["related"] = []
        draft["placement"] = ""
        rem["draft"] = draft
        rem["user_edited"] = True
        rem["user_edited_at"] = time.time()
        fresh["remediations"] = rems

    fresh = store.mutate(review_id, edit)
    if not fresh:
        raise HTTPException(status_code=404, detail="审查记录不存在或已过期")
    item = next((i for i in fresh.get("items") or [] if i.get("id") == item_id), {})
    rem = fresh["remediations"][item_id]
    return RemediateResponse(ok=True, item_id=item_id, item_name=item.get("name") or "", **rem)


@router.post(
    "/review/{review_id}/items/{item_id}/remediate/adopt",
    response_model=RemediateAdoptResponse,
)
def adopt_remediation(review_id: str, item_id: str):
    """B7-6c 标记选用（老钱 2.3）：只表态不核查——不改档位是铁律。"""
    row = store.get(review_id)
    if not row:
        raise HTTPException(status_code=404, detail="审查记录不存在")
    def adopt(fresh: dict[str, Any]) -> None:
        item = next((i for i in fresh.get("items") or []
                     if isinstance(i, dict) and i.get("id") == item_id), None)
        from app.services import llm_remediate
        if (not item or item.get("status") != "未找到"
                or llm_remediate.is_negative_item(item)
                or llm_remediate.needs_specialist(item, fresh.get("text") or "")):
            raise HTTPException(status_code=409, detail="该条目不能标记选用补写草稿")
        rems = dict(fresh.get("remediations") or {})
        rem = rems.get(item_id)
        if not isinstance(rem, dict) or rem.get("mode") != "draft":
            raise HTTPException(status_code=409,
                                detail="该条目暂无补写草稿，不能标记选用")
        rem["adopted"] = True
        rem["adopted_at"] = time.time()
        fresh["remediations"] = rems
    if store.mutate(review_id, adopt) is None:
        raise HTTPException(status_code=404, detail="审查记录已过期")
    return RemediateAdoptResponse(ok=True, item_id=item_id, adopted=True)


def _archive_ask_turn(review_id: str, request_id: str, turn_no: int,
                      result: dict, ok_flag: bool) -> None:
    """按 turn 归档（codex 十二/十三轮：谁的答案归谁的 turn——页面判废
    只管显示，持久层按 request_id 落档，绝不串写；写入失败只留日志，
    已成功且已付费的回答不因可选路径报错）。"""
    try:
        with verify_service.lock_for(review_id):
            fresh = store.get(review_id)
            if not fresh:
                return
            thread = fresh.get("ask_thread") or {"turns": []}
            turn = next((t for t in thread.get("turns") or []
                         if isinstance(t, dict) and t.get("request_id") == request_id
                         and t.get("turn_no") == turn_no), None)
            if turn is None:
                return
            turn["state"] = "success" if ok_flag else "failed"
            turn["answer"] = result.get("answer")
            # 单条超长截断（计划 §三-补2：长度上限 UI 明示）
            turn["raw_text"] = (result.get("raw_text") or "")[:8000]
            turn["error"] = result.get("error")
            turn["quote_verified"] = result.get("quote_verified")
            turn["finished_at"] = time.time()
            store.update(review_id, ask_thread=thread)
    except Exception:  # noqa: BLE001
        logger.exception("ask turn archive failed review_id=%s turn=%s",
                         review_id, turn_no)


# 领域子路由聚合（URL 不变；本文件 router 即总 router）
router.include_router(_verify_router)
router.include_router(_objection_router)
router.include_router(_ai_finding_router)
