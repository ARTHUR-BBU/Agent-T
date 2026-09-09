"""按 IP 的请求速率限制（路线图阶段 0.5）。

覆盖烧钱的 LLM 端点（POST /api/upload、POST /api/ask）——此前 /api/ask
除 500 字问题上限外零闸门，脚本刷接口等于直接烧供应商费用。

设计要点：
- 进程内滑动窗口（60s 固定，不开放配置）：单进程假设明确（uvicorn 单
  worker），多 worker 部署时限额按 worker 数放大（admin-config.md 已注明）。
- 取 request.client.host，**明确不读 X-Forwarded-For**：当前无反代直连，
  XFF 可被客户端伪造绕限频。上反代时唯一改动点 = client_ip()（届时必须
  只信任反代注入的受信 header）。
- 计数只在 allow() 返回 True 时追加：被拒请求不计入窗口，避免「重试
  风暴自己把自己的窗口填满」。
- env 调用时点读取（对齐全项目惯例，便于测试 monkeypatch）；
  0 = 关闭；垃圾值运行时回退默认并告警，启动时由 validate_rate_limit_config
  拦截（fail-closed 对齐 auth.py 先例）。
"""
from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque

from fastapi import HTTPException, Request

logger = logging.getLogger(__name__)

WINDOW_SECONDS = 60.0

# 桶数上限：超过时驱逐已滑出窗口的死键（见 allow 内注释；公网扫描器
# 会打满 /24，死键必须能被回收，字典才不会无界增长）
_MAX_BUCKETS = 512


class SlidingWindowLimiter:
    def __init__(self, env_name: str, default_limit: int) -> None:
        self._env_name = env_name
        self._default_limit = default_limit
        self._lock = threading.Lock()
        self._buckets: dict[str, deque[float]] = {}

    def limit(self) -> int:
        """读 env；未设回默认，垃圾值告警回默认，0/负数 = 关闭。"""
        raw = (os.getenv(self._env_name) or "").strip()
        if not raw:
            return self._default_limit
        try:
            val = int(raw)
        except ValueError:
            logger.warning(
                "%s=%r 不是整数，回退默认 %s", self._env_name, raw, self._default_limit
            )
            return self._default_limit
        return val if val > 0 else 0

    def allow(self, key: str, now: float | None = None) -> bool:
        """窗口内未超限返回 True 并计数；超限返回 False（不计数）。
        now 参数仅供测试注入时钟。"""
        ts = time.monotonic() if now is None else now
        limit = self.limit()
        if limit <= 0:
            return True
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = deque()
                self._buckets[key] = bucket
            cutoff = ts - WINDOW_SECONDS
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if len(bucket) >= limit:
                return False
            bucket.append(ts)
            if len(self._buckets) > _MAX_BUCKETS:
                # 驱逐死键（小智娘门禁 P2-1 整改）：只删「最后活跃时间已滑出
                # 窗口」的键与空桶，绝不清活跃桶——全局清窗会让攻击流量反而
                # 把正常 IP 的计数洗掉，与本模块防刷目的相反。
                # 字典键数随之有界：键只会在其记录仍在窗口内时存活。
                stale = [
                    k for k, b in self._buckets.items()
                    if k != key and (not b or b[-1] <= cutoff)
                ]
                for k in stale:
                    del self._buckets[k]
            return True

    def reset(self) -> None:
        with self._lock:
            self._buckets.clear()


_upload_limiter = SlidingWindowLimiter("RATE_LIMIT_UPLOAD_PER_MINUTE", 10)
_ask_limiter = SlidingWindowLimiter("RATE_LIMIT_ASK_PER_MINUTE", 20)


def client_ip(request: Request) -> str:
    # 反代接入时此处是唯一改动点：改为读受信 header（见模块 docstring）
    return request.client.host if request.client else "unknown"


def _make_dependency(limiter: SlidingWindowLimiter, message: str):
    async def _dependency(request: Request) -> None:
        if limiter.limit() <= 0:
            return
        if not limiter.allow(client_ip(request)):
            raise HTTPException(
                status_code=429,
                detail=message,
                headers={"Retry-After": "60"},
            )

    return _dependency


# 429 文案刻意区别于并发槽位的「当前审查排队已满」（一个是频率、一个是同时数），
# 且带 Retry-After 头——运维排障时两类 429 可直接区分
upload_rate_limit = _make_dependency(_upload_limiter, "上传过于频繁，请稍后再试")
ask_rate_limit = _make_dependency(_ask_limiter, "提问过于频繁，请稍后再试")


def validate_rate_limit_config() -> None:
    """启动校验（fail-closed 对齐 auth.validate_auth_config）：
    配了但不是非负整数属部署事故，拒绝启动。"""
    for limiter in (_upload_limiter, _ask_limiter):
        raw = (os.getenv(limiter._env_name) or "").strip()  # noqa: SLF001
        if not raw:
            continue
        try:
            val = int(raw)
        except ValueError:
            raise RuntimeError(
                f"环境变量 {limiter._env_name}={raw!r} 非法：必须是非负整数（0=关闭）"
            )
        if val < 0:
            raise RuntimeError(
                f"环境变量 {limiter._env_name}={raw!r} 非法：不能为负数（0=关闭）"
            )


def reset_for_tests() -> None:
    """测试辅助：清空两个 limiter 的桶（conftest teardown 用）。"""
    _upload_limiter.reset()
    _ask_limiter.reset()
