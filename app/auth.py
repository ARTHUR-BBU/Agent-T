"""HTTP Basic Auth 中间件（部署公开 URL 时的最低防护）。

- `BASIC_AUTH_USERNAME` / `BASIC_AUTH_PASSWORD` 同时配置时启用，
  除 `/health` 外所有路径（页面、静态资源、API、docs）都要求认证；
- 只配了一半属配置事故（如 HF Spaces Secret 名打错）：**启动即拒绝**
  （fail-closed，肉饼审计 P1-1——认证是公开 Space 的唯一闸门，
  静默关闭等于把上传的合同裸奔给全网，且 HF 上无人看日志）；
- 凭据逐请求读取环境变量，运行时可改（HF Spaces Secrets 重启生效），
  也便于测试用 monkeypatch 动态开关。
"""
from __future__ import annotations

import base64
import logging
import os
import secrets

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger(__name__)

# 探活路径不设防（无任何业务数据；uptime/HF 端口检查用）
_EXEMPT_PATHS = {"/health"}


def validate_auth_config() -> None:
    """启动校验：认证变量要么都配、要么都不配，半配置直接拒绝启动。"""
    username = os.environ.get("BASIC_AUTH_USERNAME", "")
    password = os.environ.get("BASIC_AUTH_PASSWORD", "")
    if bool(username) != bool(password):
        raise RuntimeError(
            "BASIC_AUTH_USERNAME/PASSWORD 必须同时配置（或同时留空）。"
            "当前只配置了一半，为避免认证静默失效导致公网裸奔，拒绝启动。"
        )


def _auth_enabled() -> bool:
    username = os.environ.get("BASIC_AUTH_USERNAME", "")
    password = os.environ.get("BASIC_AUTH_PASSWORD", "")
    return bool(username) and bool(password)


class BasicAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path in _EXEMPT_PATHS or not _auth_enabled():
            return await call_next(request)

        header = request.headers.get("Authorization", "")
        ok = False
        if header.startswith("Basic "):
            try:
                decoded = base64.b64decode(header[6:]).decode("utf-8")
                # 合并成单次比较（肉饼审计 P3-2：两次 compare_digest 的 and 短路
                # 可时序区分「用户名对/不对」）；冒号在 base64 原文里不会歧义——
                # partition 只取第一个冒号，环境变量凭据含冒号时同样成立
                username, _, password = decoded.partition(":")
                expected = (
                    os.environ["BASIC_AUTH_USERNAME"]
                    + ":"
                    + os.environ["BASIC_AUTH_PASSWORD"]
                )
                ok = secrets.compare_digest(
                    decoded.encode("utf-8"), expected.encode("utf-8")
                )
            except (ValueError, UnicodeDecodeError):
                ok = False
        if ok:
            return await call_next(request)

        # 过滤引号防止破坏 realm 语法（肉饼审计 P3-1）
        realm = os.environ.get("BASIC_AUTH_REALM", "Agent-T").replace('"', "")
        return Response(
            status_code=401,
            headers={"WWW-Authenticate": f'Basic realm="{realm}", charset="UTF-8"'},
            content="请输入访问账号和密码",
        )
