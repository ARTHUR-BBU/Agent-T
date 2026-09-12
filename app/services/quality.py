"""质量层（阶段 2.1 AI 质量分析）：规则清单之外的三维参考观察。

铁律 5（路线图）：条目不计入评分卡、永不改变规则档位；每条必须带
原文连续摘录 + 「待人工确认」。本模块输出只进 store 的 quality 键，
结构上不回流 items/scorecard（test_quality_api 用深度相等硬断言钉死）。

调用结构（对齐 model_review 阶段 1.2 形态）：
- 短合同（≤ MAX_CONTRACT_CHARS）：单次合并调用（三维度一次产出）。
- 长合同：map（按条款对齐切块复用 build_review_chunks，≤QUALITY_MAX_SEGMENTS
  块，零重试单块失败跳过）+ 成功块 ≥2 时一次「一致性轮」（跨块矛盾，
  无全文、只喂已过 quote 校验的素材——伪造原文没有原文可抄）。

注入对抗四道闸（test_quality.py 钉死）：
1. quote 全文校验（blind_spot.quote_supported）：不在原文里 → 硬丢弃；
2. 双禁语表清洗（scorecard.scrub_forbidden + llm_ask._scrub_banned_echo），
   清洗后为空 → 丢条；
3. clause_id 白名单：编造编号归一 None；
4. 封顶 12 条、每维度 ≤6；needs_confirm 不在模型输出 schema 里，代码强制 True。

失败一律软降级（QualityInfo.available=False + reason 码），绝不阻断规则引擎。
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Callable, Literal, Optional

from pydantic import BaseModel, Field

from app.prompts import quality as quality_prompts
from app.services import blind_spot, llm_ask, scorecard
from app.services.scorecard_prompts import _rule_block, build_review_chunks

logger = logging.getLogger(__name__)

ChatFn = Callable[[str, str], str]

# 观察条数封顶（宁缺毋滥；每维度 ≤6 防单维度刷屏）
MAX_OBSERVATIONS = 12
MAX_PER_DIMENSION = 6

# 字段限长（与补盲候选 MAX_QUOTE_CHARS 同量级，防把整段合同搬进报告）
MAX_TITLE_CHARS = 60
MAX_COMMENT_CHARS = 300

# 一致性轮触发门槛：成功块 <2 时跨块矛盾无从谈起
MIN_CHUNKS_FOR_CONSISTENCY = 2

# 长合同分段块数上限：QUALITY_MAX_SEGMENTS 未设回落 LLM_REVIEW_MAX_SEGMENTS
# （允许「只砍质量层延迟」的独立旋钮），垃圾值回退默认。
DEFAULT_MAX_SEGMENTS = 4

_DIMENSION_ORDER = {d: i for i, d in enumerate(quality_prompts.DIMENSIONS)}

# quote 去装饰引号（对齐 blind_spot.normalize_candidates 手法）
_QUOTE_TRIM_CHARS = "「」\"'“”"


class QualityObservation(BaseModel):
    dimension: str  # completeness / consistency / impact（清洗链白名单校验）
    title: str = ""
    quote: str = ""
    clause_id: Optional[str] = None
    comment: str = ""
    needs_confirm: bool = True  # 代码硬编码；模型输出 schema 里没有此字段


class QualityInfo(BaseModel):
    available: bool = False
    # reason: disabled / no_llm_key / llm_error / parse_failed /
    #         budget_exceeded / error / not_attempted
    reason: Optional[str] = None
    observations: list[QualityObservation] = Field(default_factory=list)
    disclaimer: str = "以上为 AI 观察，不构成审查结论、不影响逐条核查结果；每条均需人工确认。"
    dropped_count: int = 0  # quote 校验丢弃数（观测/日志用，前端不渲染）
    coverage: Optional[dict[str, Any]] = None  # 长合同 {chunks_total, chunks_reviewed, limited}


def is_quality_enabled() -> bool:
    """默认开（precheck 默认关的三条理由——同步路径/能拦审查/无预算闸——
    quality 全不占）；conftest autouse 关闭保测试确定性。"""
    return os.getenv("QUALITY_ENABLED", "true").strip().lower() not in {
        "false",
        "0",
        "no",
    }


def outcome_unavailable(reason: str) -> dict[str, Any]:
    return QualityInfo(available=False, reason=reason).model_dump()


def _max_segments() -> int:
    raw = (os.getenv("QUALITY_MAX_SEGMENTS", "") or "").strip()
    if not raw:
        raw = (os.getenv("LLM_REVIEW_MAX_SEGMENTS", "") or "").strip()
    try:
        n = int(raw)
    except ValueError:
        return DEFAULT_MAX_SEGMENTS
    return n if n >= 1 else DEFAULT_MAX_SEGMENTS


def _quality_timeout() -> float:
    """后台线程跑，不押同步请求：回落评分级 LLM_TIMEOUT_SECONDS。"""
    raw = (os.getenv("QUALITY_TIMEOUT_SECONDS", "") or "").strip()
    fallback = os.getenv("LLM_TIMEOUT_SECONDS", "180")
    try:
        return float(raw or fallback)
    except ValueError:
        return float(fallback)


def _default_chat_fn() -> Optional[ChatFn]:
    timeout = _quality_timeout()
    deepseek = llm_ask._deepseek_key()
    zhipu = llm_ask._zhipu_key()
    xai = llm_ask._xai_key()
    if deepseek:
        return lambda s, u: llm_ask._chat_deepseek(deepseek, s, u, timeout=timeout)
    if zhipu:
        return lambda s, u: llm_ask._chat_zhipu(zhipu, s, u, timeout=timeout)
    if xai:
        return lambda s, u: llm_ask._chat_xai(xai, s, u, timeout=timeout)
    return None


def run_quality(
    *,
    text: str,
    items: list[dict[str, Any]],
    policies: Optional[list[str]] = None,
    category: str = "procurement",
    clause_index: Optional[dict[str, Any]] = None,
    chat_fn: Optional[ChatFn] = None,
    budget: Optional[Any] = None,
) -> dict[str, Any]:
    """质量层主入口。任何失败绝不 raise，失败矩阵见 QualityInfo.reason。

    budget 排全链末位（预审/评分先到先得）：预算紧张时 quality 最先
    被牺牲（budget_exceeded 软降级），既有参考层优先级不被稀释。
    """
    if not is_quality_enabled():
        return outcome_unavailable("disabled")
    if not (text or "").strip():
        return outcome_unavailable("error")

    chat = chat_fn or _default_chat_fn()
    if chat is None:
        return outcome_unavailable("no_llm_key")

    policies = policies or []
    segmented = len(text) > scorecard.MAX_CONTRACT_CHARS
    observations: list[QualityObservation] = []
    dropped = 0
    coverage: Optional[dict[str, Any]] = None

    if segmented:
        chunks = build_review_chunks(text, clause_index, max_segments=_max_segments())
        if not chunks:
            return outcome_unavailable("error")
        system = quality_prompts.build_system_prompt(policies)
        chunks_total = len(chunks)
        chunks_ok = 0
        for part_no, chunk in enumerate(chunks, start=1):
            if budget is not None:
                remaining = budget.remaining()
                if remaining == 0:
                    logger.warning(
                        "Quality map stopped: budget exhausted (%d/%d chunks)",
                        part_no - 1,
                        chunks_total,
                    )
                    break
                if not budget.try_consume():
                    break
            user = quality_prompts.build_user_prompt(chunk, items, clause_index)
            try:
                raw = chat(system, user)
            except Exception:  # noqa: BLE001
                logger.exception("Quality map chunk %d/%d LLM error; skipping", part_no, chunks_total)
                continue
            parsed = _parse_observations(raw)
            if parsed is None:
                logger.warning("Quality map chunk %d/%d unparseable payload", part_no, chunks_total)
                continue
            chunks_ok += 1
            obs, dropped_n = _clean_observations(parsed, text, clause_index)
            observations.extend(obs)
            dropped += dropped_n
        coverage = {
            "chunks_total": chunks_total,
            "chunks_reviewed": chunks_ok,
            "limited": chunks_ok < chunks_total,
        }
        if chunks_ok == 0:
            # 长合同 map 全败：软降级（不回退全文单调用——超长正是要防的延迟面）
            logger.warning("Quality map produced nothing (all chunks failed)")
            return QualityInfo(
                available=False, reason="parse_failed", dropped_count=dropped, coverage=coverage
            ).model_dump()
        if chunks_ok >= MIN_CHUNKS_FOR_CONSISTENCY:
            obs, dropped_n, failed = _run_consistency_round(
                chat, system, items, observations, text, clause_index, budget
            )
            observations.extend(obs)
            dropped += dropped_n
            if failed:
                return QualityInfo(
                    available=False, reason="parse_failed", dropped_count=dropped, coverage=coverage
                ).model_dump()
    else:
        system = quality_prompts.build_system_prompt(policies)
        user = quality_prompts.build_user_prompt(text, items, clause_index)
        if budget is not None and not budget.try_consume():
            logger.warning("Quality skipped: LLM budget exhausted")
            return outcome_unavailable("budget_exceeded")
        try:
            raw = chat(system, user)
        except Exception:  # noqa: BLE001
            # exc 只进日志（对齐 llm_ask/model_review 信息泄露防线）
            logger.exception("Quality LLM error")
            return outcome_unavailable("llm_error")
        parsed = _parse_observations(raw)
        if parsed is None or _has_forbidden(parsed):
            # 主调用恰好 1 次重试（解析失败或禁语命中）
            if budget is not None and not budget.try_consume():
                logger.warning("Quality retry skipped: budget exhausted")
                return outcome_unavailable("budget_exceeded")
            retry_system = quality_prompts.build_retry_system_prompt(system)
            try:
                raw = chat(retry_system, user)
            except Exception:  # noqa: BLE001
                logger.exception("Quality retry LLM error")
                return outcome_unavailable("llm_error")
            parsed = _parse_observations(raw)
            if parsed is None:
                return outcome_unavailable("parse_failed")
        observations, dropped = _clean_observations(parsed, text, clause_index)

    if not observations:
        # 解析成功但合法观察为空：合法结果（真没有值得说的），available=True 空列表
        return QualityInfo(
            available=True, reason=None, observations=[],
            dropped_count=dropped, coverage=coverage,
        ).model_dump()

    # 去重（同 quote 或同 dimension+title 保留先到）→ 封顶 → 维度固定序
    observations = _dedupe(observations)
    observations = _cap(observations)
    observations.sort(key=lambda o: _DIMENSION_ORDER.get(o.dimension, 99))

    return QualityInfo(
        available=True, reason=None, observations=observations,
        dropped_count=dropped, coverage=coverage,
    ).model_dump()


# ---------- 一致性轮 ----------

def _run_consistency_round(
    chat: ChatFn,
    main_system: str,
    items: list[dict[str, Any]],
    map_observations: list[QualityObservation],
    text: str,
    clause_index: Optional[dict[str, Any]],
    budget: Optional[Any],
) -> tuple[list[QualityObservation], int, bool]:
    """跨块矛盾轮：无全文、素材=已过 quote 校验的观察摘要（进门先 scrub，
    防禁语经素材回流——对齐 scorecard.format_observations 先例）。

    返回 (新观察, 丢弃数, 是否失败降级)。失败降级（failed=True）时调用方
    丢弃全部产出走 parse_failed 整卡隐藏——偏保守取舍：一致性轮失败说明
    模型输出不可信，宁缺毋滥。
    """
    if budget is not None:
        remaining = budget.remaining()
        if remaining >= 0 and remaining < 1:
            logger.warning("Quality consistency round skipped: budget exhausted")
            return [], 0, False
        if not budget.try_consume():
            return [], 0, False

    material_lines = []
    for o in map_observations[:MAX_OBSERVATIONS]:
        # 防线纵深：观察虽已在清洗链洗过，素材块进门再洗一遍——禁语不得
        # 经素材回流放大重试（对齐 scorecard.format_observations 先例）
        title = llm_ask._scrub_banned_echo(scorecard.scrub_forbidden(o.title))
        comment = llm_ask._scrub_banned_echo(scorecard.scrub_forbidden(o.comment))
        material_lines.append(f"- [{o.dimension}] {title}｜原文：{o.quote}｜{comment}")
    system = quality_prompts.build_system_prompt([])
    user = quality_prompts.build_consistency_user_prompt(
        _rule_block(items), "\n".join(material_lines)
    )
    try:
        raw = chat(system, user)
    except Exception:  # noqa: BLE001
        logger.exception("Quality consistency round LLM error")
        return [], 0, True
    parsed = _parse_observations(raw)
    if parsed is None or _has_forbidden(parsed):
        if budget is not None and not budget.try_consume():
            return [], 0, False
        try:
            raw = chat(quality_prompts.build_retry_system_prompt(system), user)
        except Exception:  # noqa: BLE001
            logger.exception("Quality consistency retry LLM error")
            return [], 0, True
        parsed = _parse_observations(raw)
        if parsed is None:
            return [], 0, True
    obs, dropped = _clean_observations(parsed, text, clause_index)
    return obs, dropped, False


# ---------- 解析与清洗链（注入对抗核心，顺序固定） ----------

def _parse_observations(raw: str) -> Optional[list[dict[str, Any]]]:
    """fence 剥壳 → json.loads → observations 列表。None=结构错误（触发重试），
    [] = 合法空。"""
    if not raw:
        return None
    text = raw.strip()
    fence = re.match(r"^```(?:json)?\s*([\s\S]*?)\s*```$", text)
    if fence:
        text = fence.group(1).strip()
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{[\s\S]*\}", text)
        if not m:
            return None
        try:
            obj = json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
    obs = obj.get("observations") if isinstance(obj, dict) else None
    if not isinstance(obs, list):
        return None
    return [o for o in obs if isinstance(o, dict)]


def _has_forbidden(parsed: list[dict[str, Any]]) -> bool:
    blob = " ".join(
        str(o.get("title") or "") + str(o.get("comment") or "") for o in parsed
    )
    return bool(scorecard.check_forbidden(blob) or llm_ask._scrub_banned_echo(blob) != blob)


def _clean_observations(
    parsed: list[dict[str, Any]],
    text: str,
    clause_index: Optional[dict[str, Any]],
) -> tuple[list[QualityObservation], int]:
    """清洗链：白名单→限长→clause_id 归一→quote 全文校验→双禁语→needs_confirm。

    返回 (合法观察, quote 校验丢弃数)。
    """
    valid_ids = {
        str(c.get("id"))
        for c in ((clause_index or {}).get("clauses") or [])
        if c.get("id")
    }
    out: list[QualityObservation] = []
    dropped = 0
    for row in parsed:
        dimension = str(row.get("dimension") or "").strip()
        if dimension not in quality_prompts.DIMENSIONS:
            continue
        quote = str(row.get("quote") or "").strip().strip(_QUOTE_TRIM_CHARS).strip()
        if not quote or not blind_spot.quote_supported(text, quote):
            dropped += 1
            continue
        title = str(row.get("title") or "").strip()[:MAX_TITLE_CHARS]
        comment = str(row.get("comment") or "").strip()[:MAX_COMMENT_CHARS]
        # 双禁语表（评分禁语 + 追问诱导禁语）
        title = llm_ask._scrub_banned_echo(scorecard.scrub_forbidden(title)).strip()
        comment = llm_ask._scrub_banned_echo(scorecard.scrub_forbidden(comment)).strip()
        # 清洗打空 → 丢条；残留【已过滤】碎片（如「没问题，可以盖章通过。」
        # 只命中「可以盖章」剩「通过。」）同样丢条——半清洗的话术不给用户看
        if not title or "【已过滤】" in title:
            continue
        if not comment or "【已过滤】" in comment:
            continue
        cid = str(row.get("clause_id") or "").strip()
        clause_id = cid if cid in valid_ids else None
        out.append(
            QualityObservation(
                dimension=dimension,
                title=title,
                quote=quote[: blind_spot.MAX_QUOTE_CHARS],
                clause_id=clause_id,
                comment=comment,
                needs_confirm=True,  # 代码强制；模型无权声明免确认
            )
        )
    return out, dropped


def _dedupe(observations: list[QualityObservation]) -> list[QualityObservation]:
    seen_quotes: set[str] = set()
    seen_pairs: set[tuple[str, str]] = set()
    out: list[QualityObservation] = []
    for o in observations:
        if o.quote in seen_quotes:
            continue
        pair = (o.dimension, o.title)
        if pair in seen_pairs:
            continue
        seen_quotes.add(o.quote)
        seen_pairs.add(pair)
        out.append(o)
    return out


def _cap(observations: list[QualityObservation]) -> list[QualityObservation]:
    per_dim: dict[str, int] = {}
    out: list[QualityObservation] = []
    for o in observations:
        if len(out) >= MAX_OBSERVATIONS:
            break
        n = per_dim.get(o.dimension, 0)
        if n >= MAX_PER_DIMENSION:
            continue
        per_dim[o.dimension] = n + 1
        out.append(o)
    return out
