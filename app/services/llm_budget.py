"""单次审查生命周期的 LLM 调用预算（路线图阶段 0.5）。

为什么先立闸：现状单次审查常态 2 次（预审 1 + 评分/补盲合并 1）、最坏 4 次
（各自重试）；阶段 2 质量层将引入按条款分段阅读，调用数会翻数倍。预算
上限必须在调用数膨胀之前存在，超限走既有软降级（评分卡 unavailable /
预审 skip），绝不硬失败——规则引擎永远照常。

设计要点：
- 记账挂调用点（precheck/model_review 的检查点），不挂 _chat_* 内部——
  /api/ask 走 _chat_* 但不计预算，挂内部会误伤用户手动追问。
- Budget 对象在 upload 请求内创建、按引用贯穿预审线程与审查线程，
  随 GC 清理：无注册表即无泄漏、无清理时机问题。force 重传天然新预算。
- 线程安全：预审跑在 run_in_threadpool，审查跑在 daemon 线程，
  try_consume 必须原子 check-and-decrement。

占位（本期不做）：/api/ask 的每审查调用上限（LLM_ASK_MAX_PER_REVIEW）——
需先解决按 review_id 进程内计数的清理时机（store TTL 清理钩子不在
ask 路径上）；ask 当前由 IP 限频（rate_limit）覆盖。
"""
from __future__ import annotations

import logging
import os
import threading

logger = logging.getLogger(__name__)

# 默认 12 = 现状最坏 4 次的 3 倍余量；阶段 2 分段阅读时再按实际调优。
# 注意：配置 < 4 会影响现状最坏路径（重试被预算截断），文档已注明。
DEFAULT_BUDGET = 12


class ReviewBudget:
    """线程安全的单次审查 LLM 调用计数器。limit <= 0 视为不限。"""

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._lock = threading.Lock()
        self._left = limit

    def try_consume(self, n: int = 1) -> bool:
        """原子扣减；余额不足返回 False（调用方走软降级），不部分扣减。"""
        with self._lock:
            if self._limit <= 0:  # 不限
                return True
            if self._left < n:
                return False
            self._left -= n
            return True

    def remaining(self) -> int:
        with self._lock:
            return self._left if self._limit > 0 else -1  # -1 = 不限


def limit_from_env() -> int:
    """读 LLM_BUDGET_PER_REVIEW；垃圾值回退默认（运行时兜底）；
    0 或负数 = 不限。"""
    raw = (os.getenv("LLM_BUDGET_PER_REVIEW") or "").strip()
    if not raw:
        return DEFAULT_BUDGET
    try:
        val = int(raw)
    except ValueError:
        logger.warning("LLM_BUDGET_PER_REVIEW=%r 不是整数，回退默认 %s", raw, DEFAULT_BUDGET)
        return DEFAULT_BUDGET
    return val if val > 0 else 0


def new_review_budget() -> ReviewBudget | None:
    """每次 upload 请求调用一次；关闭态（limit<=0）返回 None，全程零开销直通。"""
    limit = limit_from_env()
    if limit <= 0:
        return None
    return ReviewBudget(limit)
