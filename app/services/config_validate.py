"""LLM 运行时配置的启动校验（宪法 P0-C2）。

背景（宪法一致性审计 C2）：LLM_TIMEOUT_SECONDS 非法值此前在**调用期**
float() 抛错（第一次用才炸）；LLM_BUDGET_PER_REVIEW / LLM_REVIEW_MAX_SEGMENTS
垃圾值运行时静默回退。规范 18 节把「结构性配置错误」归入 fail-closed——
配置写坏应在启动时显式拒绝（对齐 STORE_TTL 先例，PR #30），而不是让
第一次真实请求背锅或悄悄降级。

设计：main.py 启动时调用本函数；运行时函数内部的防御性回退保留
（双保险：启动拦一道，函数内再兜一道）。
"""
from __future__ import annotations

import os

# float 型配置（秒）
_FLOAT_KEYS = (
    "LLM_TIMEOUT_SECONDS",
    "PRECHECK_TIMEOUT_SECONDS",
    "QUALITY_TIMEOUT_SECONDS",
    "OBJECTION_TIMEOUT_SECONDS",
)
# int 型配置（次数），0 或正整数合法；负数与垃圾值拒绝
_INT_KEYS = (
    "LLM_BUDGET_PER_REVIEW",
    "LLM_REVIEW_MAX_SEGMENTS",
    "QUALITY_MAX_SEGMENTS",
    "RATE_LIMIT_UPLOAD_PER_MINUTE",
    "RATE_LIMIT_ASK_PER_MINUTE",
)


def validate_llm_runtime_config() -> None:
    """启动期校验 LLM 运行时配置；非法即 raise（fail-closed）。"""
    problems: list[str] = []
    for name in _FLOAT_KEYS:
        raw = (os.getenv(name) or "").strip()
        if not raw:
            continue
        try:
            if float(raw) <= 0:
                problems.append(f"{name}={raw!r} 必须为正数")
        except ValueError:
            problems.append(f"{name}={raw!r} 不是合法数字")
    for name in _INT_KEYS:
        raw = (os.getenv(name) or "").strip()
        if not raw:
            continue
        try:
            if int(raw) < 0:
                problems.append(f"{name}={raw!r} 不能为负数")
        except ValueError:
            problems.append(f"{name}={raw!r} 不是合法整数")
    if problems:
        raise ValueError(
            "LLM 运行时配置校验失败（fail-closed，修正环境变量后重启）：\n- "
            + "\n- ".join(problems)
        )
