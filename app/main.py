"""FastAPI entry: contract review Agent MVP."""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api.routes import router
from app.auth import BasicAuthMiddleware, validate_auth_config
from app.services.checklist import validate_checklist_configs
from app.services.rate_limit import validate_rate_limit_config

# 半配置的认证变量属部署事故，启动即拒绝（肉饼审计 P1-1，fail-closed）
validate_auth_config()
# 限频变量同理：配了但不是非负整数 = 部署事故，启动即拒绝（阶段 0.5）
validate_rate_limit_config()
# 清单正则/id 启动校验（外部审计批2）：配置坏了宁可起不来，不悄悄降级漏审
validate_checklist_configs()

STATIC_DIR = Path(__file__).resolve().parent / "static"

# 请求体总上限（业务文件 10MB + multipart 编码余量）。Content-Length 缺失/
# chunked 时也按流式累计字节拒绝，避免整包进 Starlette multipart 临时盘。
# 反向代理（nginx client_max_body_size）仍建议同步配置作最外层防线。
MAX_REQUEST_BODY_BYTES = 12 * 1024 * 1024


class _BodyTooLarge(Exception):
    """Internal: streaming receive exceeded MAX_REQUEST_BODY_BYTES."""


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


app = FastAPI(title="合同审查 Agent", version="0.1.0")
app.add_middleware(BodySizeLimitMiddleware)
app.include_router(router)

# 公网部署时配 BASIC_AUTH_USERNAME/PASSWORD 启用整站 Basic Auth（/health 除外）
app.add_middleware(BasicAuthMiddleware)

if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/")
def index():
    index_path = STATIC_DIR / "index.html"
    if index_path.exists():
        return FileResponse(index_path)
    return {"message": "合同审查 Agent MVP", "docs": "/docs"}
