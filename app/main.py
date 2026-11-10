"""FastAPI entry: contract review Agent MVP."""
from __future__ import annotations

from pathlib import Path
from contextlib import asynccontextmanager
import asyncio
import logging
import os

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api.routes import router
from app.api.routes_revision import router as revision_router
from app.auth import BasicAuthMiddleware, validate_auth_config
from app.api.local_access import require_loopback
from app.api.routes_local_accounts import _guard as _trial_account_guard
from app.services.checklist import validate_checklist_configs
from app.services.config_validate import validate_llm_runtime_config
from app.services.rate_limit import validate_rate_limit_config
from app.services import store as store_module
from app.services import owned_review_maintenance

# 半配置的认证变量属部署事故，启动即拒绝（肉饼审计 P1-1，fail-closed）
validate_auth_config()
# 限频变量同理：配了但不是非负整数 = 部署事故，启动即拒绝（阶段 0.5）
validate_rate_limit_config()
# 清单正则/id 启动校验（外部审计批2）：配置坏了宁可起不来，不悄悄降级漏审
validate_checklist_configs()
validate_llm_runtime_config()

STATIC_DIR = Path(__file__).resolve().parent / "static"
logger = logging.getLogger(__name__)

# 请求体总上限（业务文件 10MB + multipart 编码余量）。Content-Length 缺失/
# chunked 时也按流式累计字节拒绝，避免整包进 Starlette multipart 临时盘。
# 反向代理（nginx client_max_body_size）仍建议同步配置作最外层防线。
MAX_REQUEST_BODY_BYTES = 12 * 1024 * 1024


class _BodyTooLarge(Exception):
    """Internal: streaming receive exceeded MAX_REQUEST_BODY_BYTES."""


class OwnedReviewNoStoreMiddleware:
    """Prevent caching successful or failed responses containing account context."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        prefix = "/api/local-accounts/contracts"
        if scope["type"] != "http" or not (path == prefix or path.startswith(prefix + "/")):
            await self.app(scope, receive, send)
            return

        started = False

        async def no_store_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                headers = [(k, v) for k, v in message.get("headers", []) if k.lower() != b"cache-control"]
                message["headers"] = headers + [(b"cache-control", b"no-store")]
            await send(message)

        try:
            await self.app(scope, receive, no_store_send)
        except Exception as exc:
            if started:
                raise
            logger.error("账户合同试验请求意外失败：%s", type(exc).__name__)
            await JSONResponse({"detail": "账户合同试验暂不可用"}, status_code=500,
                               headers={"Cache-Control": "no-store"})(scope, receive, send)


class LocalRuleTrialMiddleware:
    """The explicitly enabled experiment is never reachable through a proxy or network peer."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        mode = os.getenv("RULE_EXECUTION_DB_TRIAL", "0")
        if mode not in ("", "0", "1"):
            await JSONResponse({"detail": "规则试验配置无效"}, status_code=503,
                               headers={"X-Rule-Execution-Trial": "invalid", "Cache-Control": "no-store"})(scope, receive, send)
            return
        if mode == "1":
            try:
                require_loopback(Request(scope), write=scope.get("method", "GET") not in {"GET", "HEAD", "OPTIONS"})
                path = _trial_account_guard(Request(scope))
                if not path.is_file():
                    raise HTTPException(503, "试验数据库不存在")
            except (HTTPException, OSError, ValueError) as exc:
                status = 403 if isinstance(exc, HTTPException) and exc.status_code == 403 else 503
                await JSONResponse({"detail": "规则试验配置无效或访问不允许"}, status_code=status,
                                   headers={"X-Rule-Execution-Trial": "database_test", "Cache-Control": "no-store"})(scope, receive, send)
                return
            request_path = scope.get("path", "")
            if request_path == "/api/rule-drafts" or request_path.startswith("/api/rule-drafts/"):
                await JSONResponse({"detail": "试验模式不使用文件规则草稿"}, status_code=404)(scope, receive, send)
                return
            async def trial_send(message):
                if message["type"] == "http.response.start":
                    headers = message.setdefault("headers", [])
                    headers.append((b"x-rule-execution-trial", b"database_test"))
                    if request_path.startswith("/api/"):
                        message["headers"] = [(key, value) for key, value in headers if key.lower() != b"cache-control"] + [(b"cache-control", b"no-store")]
                await send(message)

            await self.app(scope, receive, trial_send)
            return
        await self.app(scope, receive, send)


class AccountModeBoundaryMiddleware:
    """Close legacy contract APIs and require direct loopback in account mode."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or store_module._ACCOUNT_MODE != "1":
            await self.app(scope, receive, send)
            return
        try:
            require_loopback(Request(scope), write=scope.get("method", "GET") not in {"GET", "HEAD", "OPTIONS"})
        except HTTPException:
            await JSONResponse({"detail": "仅允许本机直接访问"}, status_code=403,
                               headers={"Cache-Control": "no-store"})(scope, receive, send)
            return
        path = scope.get("path", "")
        account = path == "/api/local-accounts" or path.startswith("/api/local-accounts/")
        public = path in {"/api/categories", "/api/rules/catalog", "/api/rules/catalog/capability"}
        if path.startswith("/api/") and not (account or public):
            await JSONResponse({"detail": "账户模式不提供旧合同接口"}, status_code=404,
                               headers={"Cache-Control": "no-store"})(scope, receive, send)
            return
        await self.app(scope, receive, send)


class BodySizeLimitMiddleware:
    """ASGI：在 multipart 解析前按流累计请求体，超限立即 413。

    旧 @app.middleware 只看 Content-Length，chunked/无 CL 时 UploadFile 已
    持有完整 body 才进 _read_limited。本中间件包装 receive，超限不再继续
    拉取后续 chunk（best-effort；代理层仍建议设 client_max_body_size）。
    """

    def __init__(self, app, max_bytes: int = MAX_REQUEST_BODY_BYTES):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {k.lower(): v for k, v in (scope.get("headers") or [])}
        cl = headers.get(b"content-length")
        if cl is not None:
            try:
                if int(cl.decode("latin-1")) > self.max_bytes:
                    await self._send_413(send)
                    return
            except ValueError:
                pass

        received = 0
        rejected = False

        async def limited_receive():
            nonlocal received, rejected
            if rejected:
                return {"type": "http.disconnect"}
            message = await receive()
            if message["type"] == "http.request":
                chunk = message.get("body", b"") or b""
                received += len(chunk)
                if received > self.max_bytes:
                    rejected = True
                    raise _BodyTooLarge()
            return message

        try:
            await self.app(scope, limited_receive, send)
        except _BodyTooLarge:
            await self._send_413(send)

    @staticmethod
    async def _send_413(send):
        body = '{"detail":"请求体积超过上限，请压缩后重试"}'.encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    [b"content-type", b"application/json; charset=utf-8"],
                    [b"content-length", str(len(body)).encode("ascii")],
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


@asynccontextmanager
async def lifespan(_app):
    stop = asyncio.Event()
    task = asyncio.create_task(owned_review_maintenance.run(stop)) if owned_review_maintenance.enabled() else None
    try:
        yield
    finally:
        if task is not None:
            stop.set()
            await task


app = FastAPI(title="合同审查 Agent", version="0.1.0", lifespan=lifespan)
app.add_middleware(BodySizeLimitMiddleware)
app.add_middleware(LocalRuleTrialMiddleware)
app.add_middleware(AccountModeBoundaryMiddleware)
app.include_router(router)
app.include_router(revision_router)

# 公网部署时配 BASIC_AUTH_USERNAME/PASSWORD 启用整站 Basic Auth（/health 除外）
app.add_middleware(BasicAuthMiddleware)
app.add_middleware(OwnedReviewNoStoreMiddleware)

if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/")
def index():
    index_path = STATIC_DIR / ("owned_workspace.html" if owned_review_maintenance.enabled() else "index.html")
    if index_path.exists():
        return FileResponse(index_path)
    return {"message": "合同审查 Agent MVP", "docs": "/docs"}


@app.get("/account-management")
def account_management():
    return FileResponse(STATIC_DIR / "index.html")
