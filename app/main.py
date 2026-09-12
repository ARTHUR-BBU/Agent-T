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

app = FastAPI(title="合同审查 Agent", version="0.1.0")
app.include_router(router)


@app.middleware("http")
async def request_body_limit(request, call_next):
    """HTTP body 总限制（外部审计二轮 PR-C：三层防线最外层）。

    _read_limited 管的是「应用不把超大文件读进内存」，这层管的是「超大
    请求在 multipart 解析前就被拒」——Content-Length 谎报/缺失时仍由
    _read_limited 兜底。上限给业务 10MB 留 multipart 编码余量。
    """
    content_length = request.headers.get("content-length")
    if content_length and content_length.isdigit():
        if int(content_length) > 12 * 1024 * 1024:
            from fastapi.responses import JSONResponse

            return JSONResponse(
                status_code=413, content={"detail": "请求体积超过上限，请压缩后重试"}
            )
    return await call_next(request)


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
