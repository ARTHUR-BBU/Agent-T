"""FastAPI entry: contract review Agent MVP."""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api.routes import router
from app.auth import BasicAuthMiddleware, validate_auth_config

# 半配置的认证变量属部署事故，启动即拒绝（肉饼审计 P1-1，fail-closed）
validate_auth_config()

STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="合同审查 Agent", version="0.1.0")
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
