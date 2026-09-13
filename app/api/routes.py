"""API routers: upload / review / ask / report."""
from __future__ import annotations

import logging
import threading
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile
from fastapi.concurrency import run_in_threadpool

from app.api.schemas import (
    AskRequest,
    AskResponse,
    ClauseIndexInfo,
    PrecheckInfo,
    QualityInfo,
    ReviewSummary,
    ScorecardInfo,
    UploadResponse,
)
from app.graph.pipeline import run_review
from app.services import llm_ask, precheck as precheck_service, report as report_service
from app.services import llm_budget, rate_limit
from app.services.checklist import list_categories
from app.services.clause_index import build_clause_context
from app.services.extract import ExtractionError, extract_text
from app.services import stance as stance_service
from app.services.store import store

router = APIRouter(prefix="/api")

logger = logging.getLogger(__name__)

# 上传硬限制（外部审计 P1：无限制的大文件可耗尽内存/模型费用）
MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10MB
ALLOWED_SUFFIXES = {".txt", ".md", ".text", ".pdf", ".docx", ".doc"}

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


# 限频依赖先于 handler 执行：超频请求在读文件/预审之前就被廉价拒绝（阶段 0.5）
def _start_review(
    *,
    filename: str,
    raw: bytes,
    category: str,
    stance: str,
    budget: object | None,
    precheck_record: dict | None,
) -> str:
    """建档 + 启动后台审查线程（外部审计二轮 PR-C 从 upload 抽取）。

    槽位所有权约定：调用方已 acquire _review_slots；本函数任何启动前异常
    直接上抛（调用方负责还槽），线程成功启动后所有权转移给 worker 的
    finally——「一定成对释放」不依赖 worker 是否真的跑起来。
    """
    rid = store.create(
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
        text="",
        policies=[],
        error=None,
        precheck=precheck_record,
    )

    # 审查放后台线程：上传立即返回 review_id（外部审计 P1：同步等待模型
    # 会拖死请求，前端的 processing 轮询此前形同虚设）
    def _run_review_worker(
        review_id: str, fname: str, content: bytes, cat: str, budget: object | None
    ) -> None:
        # stage 回调：pipeline 节点入口上报 → 逐步落库；update 自身有兜底，
        # 回调失败不影响审查（pipeline 侧还包了一层 try/except）
        def on_stage(stage: str) -> None:
            store.update(review_id, stage=stage)

        try:
            try:
                result = run_review(fname, content, category=cat, budget=budget, on_stage=on_stage)
                if result.get("error"):
                    store.update(
                        review_id,
                        status="error",
                        stage="error",
                        error=result["error"],
                        text=result.get("text") or "",
                        quality=result.get("quality") or {},
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
                        text=result.get("text") or "",
                        policies=result.get("policies") or [],
                        category=result.get("category") or cat,
                        category_label=result.get("category_label") or cat,
                        clause_index=result.get("clause_index"),
                        text_preview=preview,
                        error=None,
                    )
            except Exception:  # noqa: BLE001
                logger.exception("后台审查失败 review_id=%s", review_id)
                store.update(review_id, status="error", stage="error", error="审查失败，请重新上传")
        except Exception:  # noqa: BLE001
            # 兜底（小智娘 P3-3）：update 自身失败（如 DB 锁超时）不得裸抛线程
            logger.exception("后台审查状态写入失败 review_id=%s", review_id)
        finally:
            _review_slots.release()

    threading.Thread(
        target=_run_review_worker, args=(rid, filename, raw, category, budget), daemon=True
    ).start()
    return rid


@router.post(
    "/upload",
    response_model=UploadResponse,
    dependencies=[Depends(rate_limit.upload_rate_limit)],
)
async def upload(
    file: UploadFile = File(...),
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

    # 流式限额（外部审计批1-②）：分块读取，累计超限立即拒收——
    # 旧实现 await file.read() 先把整个文件读进内存才判长度，「10MB 以上
    # 不给审」做到了，「10MB 以上不进内存」没做到
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

    # 单次审查 LLM 预算（阶段 0.5）：每个 upload 请求一个 Budget 对象，
    # 按引用贯穿预审与审查线程，随 GC 清理；force 重传天然新预算。
    # 创建于预审之前（预审也计入预算），关闭态返回 None 全程直通。
    budget = llm_budget.new_review_budget()

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
    if not _parse_slots.acquire(blocking=False):
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
    if contract_text is not None:
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
            return UploadResponse(
                message="category_confirm",
                status="category_confirm",
                precheck=PrecheckInfo(
                    performed=True,
                    detected_type=r.detected_type,
                    confidence=r.confidence,
                    summary=r.summary,
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
            }

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
    clause_index = row.get("clause_index")
    stance = row.get("stance") or "neutral"
    quality = row.get("quality")
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
    if not item:
        raise HTTPException(status_code=404, detail="清单项不存在")

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
    )
    return AskResponse(
        ok=bool(result.get("ok")),
        item_id=item.get("id", body.item_id),
        item_name=item.get("name", ""),
        answer=result.get("answer"),
        raw_text=result.get("raw_text"),
        error=result.get("error"),
    )
