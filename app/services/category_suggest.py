# -*- coding: utf-8 -*-
"""B2-3 批3：分诊台快思考建议层（施工卡 spec-b23-triage-integration.md）。

红线（spec-b2 §3.1 硬接口）：
- 模型只给「建议品类」，永不切换；建议与用户选择不一致时一律由路由层
  触发 category_confirm（置信度只影响文案与默认选项，不决定弹不弹）。
- 留痕八字段：provider/确切模型版本/提示词哈希/输入覆盖范围/延迟/费用/
  原始结构化输出/降级原因。
- 外部模型开关（CATEGORY_SUGGEST_ENABLED）**默认关**：关=零网络请求，
  行为与现状完全一致。
- 拒收话术（老钱签认 2026-10-16）：「暂不在支持范围」，禁「有风险/不合规」。

选型演进：Jev（29/29，$0.041/M）→ 生产 451 地域封锁熔断（2026-10-18）
→ **Cloudflare Clef 主通道**（用户 2026-10-18 提供凭据，生产服务器实测可达；
接口与 Jev 同构，输出 0 计费）。Clef 同卷校准见 docs/b23/ 三模型对比。
校准依据：docs/b23/threshold-analysis-jev-r1.md。
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Optional

from pydantic import BaseModel

from app.services import llm_client

PROMPT_VERSION = "cat-suggest-v1"

# Jev 判定指令与八类定义（与校准批次 shadow_jev_calibration_20261017_173606
# 的实际发送内容逐字节一致——提示词哈希纪律）
JEV_INSTRUCTIONS = ("判断这份合同文本属于哪个品类。依据标题、当事人称谓与核心义务；"
                    "空白模板按栏目结构判断。")
JEV_CRITERIA = {
    "procurement": "买卖/采购/供货合同", "lease": "租赁合同", "nda": "保密协议",
    "technical_service": "技术服务合同（以技术知识解决特定技术问题）",
    "consulting": "咨询合同（含技术/工程/管理咨询）",
    "labor_dispatch": "劳务派遣/人力外包（三方结构）",
    "tech_transfer": "技术转让/许可（权属移转或授权）", "other": "以上均不是",
}

CATEGORIES = ("procurement", "lease", "nda", "technical_service",
              "consulting", "labor_dispatch", "tech_transfer", "other")

_HEAD_CHARS = 6000
_TAIL_CHARS = 2000
_CLIP_MARKER = "\n...[中段截断]...\n"

# 老钱签认拒收话术（禁「有风险/不合规」；用户可读文案不露英文 id）
_CATEGORY_ZH = {
    "procurement": "采购合同", "lease": "租赁合同", "nda": "保密协议",
    "technical_service": "技术服务合同", "consulting": "咨询合同",
    "labor_dispatch": "劳务派遣合同", "tech_transfer": "技术转让/许可合同",
    "other": "其他类型合同",
}


class Suggestion(BaseModel):
    category: Optional[str] = None  # 八类之一；解析失败/白名单外=None
    confidence: Optional[float] = None


class SuggestOutcome(BaseModel):
    performed: bool = False
    # skip_reason: disabled / no_llm_key / llm_error / parse_failed
    skip_reason: Optional[str] = None
    coverage: Optional[dict[str, Any]] = None
    result: Optional[Suggestion] = None
    # 八字段留痕（spec-b2 硬接口 4）
    provider: Optional[str] = None
    model_version: Optional[str] = None
    attempt: int = 1
    latency_s: Optional[float] = None
    cost_note: Optional[str] = None
    raw_output: Optional[str] = None


def is_enabled() -> bool:
    """外部模型开关，默认关（关=零网络请求）。"""
    return os.getenv("CATEGORY_SUGGEST_ENABLED", "false").strip().lower() in {
        "true", "1", "yes",
    }


def _clip_for_suggest(text: str) -> tuple[str, str]:
    """截断策略与校准同构（头6000+尾2000，§5 协议）；coverage 记账。"""
    if len(text) <= _HEAD_CHARS:
        return text, "full"
    clipped = text[:_HEAD_CHARS] + _CLIP_MARKER + text[-_TAIL_CHARS:]
    return clipped, f"truncated_head_{_HEAD_CHARS}_tail_{_TAIL_CHARS}"


def confidence_tier(confidence: float) -> str:
    """置信度分档：≥0.90 high，<0.90 low（0.90 单归属高置信）。"""
    return "high" if confidence >= 0.90 else "low"


def category_zh(category_id: str) -> str:
    """用户可读中文品类名（公共接口，路由层经此取文案不摸私有映射）。"""
    return _CATEGORY_ZH.get(category_id, category_id)


def reject_notice(category_id: str) -> str:
    """拒收话术（老钱签认口径）：不说「有风险/不合规」——只说不在支持范围。"""
    zh = _CATEGORY_ZH.get(category_id, "其他类型合同")
    return f"您上传的合同疑似{zh}，本产品当前支持采购、租赁、保密协议、技术服务四类，暂不在支持范围。"


def suggest_category(text: str, selected_category: str) -> SuggestOutcome:
    """快思考建议（同步调用，路由层用 run_in_threadpool 包裹）。"""
    outcome = SuggestOutcome()
    if not is_enabled():
        outcome.skip_reason = "disabled"
        return outcome
    env = _load_env()
    key = env.get("CF_API_TOKEN")
    if not key:
        outcome.skip_reason = "no_llm_key"
        return outcome

    clipped, coverage = _clip_for_suggest(text)
    outcome.coverage = {"coverage": coverage, "selected_category": selected_category}
    base = env.get("CF_AI_BASE", "https://api.cloudflare.com/client/v4").rstrip("/")
    account = env.get("CF_ACCOUNT_ID", "")
    # P1-1（复核）：默认=clef-flash（校准同款+用户拍板最便宜档）——生产漏配
    # CF_CLEF_MODEL 时不得静默升级 27B 贵档
    model = env.get("CF_CLEF_MODEL", "clef-flash")
    outcome.provider = "clef"
    outcome.model_version = model
    if not account:
        # P1-1（复核）：缺 CF_ACCOUNT_ID 明确降级，不拼空账户 URL
        outcome.skip_reason = "no_account_id"
        return outcome

    # P1-1（肉饼门禁）：transport 走 llm_client.typesafe_judgment 单点——
    # LLMCallRecord 账本只在彼处落盘，自建 HTTP 会绕过账本留痕空转
    t0 = time.time()
    raw: Optional[str] = None
    last_err: Optional[Exception] = None
    # P1-2（复核）：瞬态网络错误（断连/超时）有限重试一次——首轮受控试运行
    # 留出集 1 例 RemoteDisconnected 即此类；白名单解析失败不重试
    for attempt in (1, 2):
        try:
            raw = llm_client.decision_judgment(
                api_key=key, url=f"{base}/accounts/{account}/ai/run/@cf/cloudflare/{model}",
                model=model, state=clipped, instructions=JEV_INSTRUCTIONS,
                criteria=dict(JEV_CRITERIA), timeout=15.0,
                truncated=(coverage != "full"),
            )
            last_err = None
            outcome.skip_reason = None  # 重试成功须清前次失败残留
            outcome.attempt = attempt
            break
        except Exception as e:
            last_err = e
            outcome.skip_reason = f"llm_error: {type(e).__name__}"
            outcome.attempt = attempt
    outcome.latency_s = round(time.time() - t0, 2)
    if last_err is not None or raw is None:  # 两次尝试均失败才降级
        outcome.raw_output = str(last_err)[:300]
        return outcome
    outcome.cost_note = "usage 由 llm_client 账本记录（LLMCallRecord.usage）"
    # P2-5（肉饼门禁）：畸形响应 fail-closed——结构异常按 parse_failed 降级，
    # 绝不上穿 500 阻断上传主流程
    outcome.raw_output = (raw or "")[:500]

    parsed = _parse_verdict(raw)
    if parsed is None:
        outcome.skip_reason = "parse_failed"
        return outcome
    outcome.performed = True
    outcome.result = parsed
    return outcome


def _load_env() -> dict[str, str]:
    from pathlib import Path
    env: dict[str, str] = {}
    dot_env = Path(__file__).resolve().parents[2] / ".env"
    if dot_env.exists():
        for line in dot_env.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                v = v.strip()
                # P3（肉饼门禁）：剥行内引号（KEY="v" 场景）
                if len(v) >= 2 and v[0] == v[-1] and v[0] in {'"', "'"}:
                    v = v[1:-1]
                env[k.strip()] = v
    env.update({k: v for k, v in os.environ.items()
                if k.startswith(("CF_", "CATEGORY_SUGGEST"))})
    return env


def _parse_verdict(raw: str) -> Optional[Suggestion]:
    """JSON 解析+白名单校验（注入逃逸防线：白名单外一律 None）。"""
    text = (raw or "").strip()
    # Jev answers 本身是完整 JSON（可含嵌套 probabilities）——先整体 loads，
    # 失败再退正则截取（容忍围栏/前后杂讯）
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        fence = re.match(r"^```(?:json)?\s*([\s\S]*?)\s*```$", text)
        if fence:
            text = fence.group(1).strip()
        m = re.search(r"\{[\s\S]*\}", text)
        if not m:
            return None
        try:
            obj = json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
    if not isinstance(obj, dict):
        return None
    # Jev answers.category 形态：{type, choice, probabilities, confidence}
    category = obj.get("choice") or obj.get("category")
    if category not in CATEGORIES:
        return None
    try:
        conf = max(0.0, min(1.0, float(obj.get("confidence", 0))))
    except (TypeError, ValueError):
        conf = 0.0
    return Suggestion(category=category, confidence=conf)
