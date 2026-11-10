"""Shared direct-loopback guard for explicitly enabled local development APIs."""
from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit

from fastapi import HTTPException, Request


def require_loopback(request: Request, *, write: bool = False) -> None:
    try:
        peer = ipaddress.ip_address(request.client.host if request.client else "")
        host = urlsplit("//" + request.headers.get("host", "")).hostname
    except ValueError as exc:
        raise HTTPException(403, "仅允许本机访问") from exc
    if (not peer.is_loopback or host not in {"127.0.0.1", "localhost", "::1"}
            or any(h in request.headers for h in ("forwarded", "x-forwarded-for", "x-forwarded-host", "x-forwarded-proto"))
            or request.headers.get("sec-fetch-site") == "cross-site"):
        raise HTTPException(403, "仅允许本机直接访问")
    if write and request.headers.get("origin") != str(request.base_url).rstrip("/"):
        raise HTTPException(403, "请求来源不匹配")
