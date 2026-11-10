"""Explicit loopback-only account experiment; it does not protect contract APIs."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from app.api.local_access import require_loopback
from app.services.account_store import AccountStore, AuthenticationError, AuthorizationError
from app.services.store import store as review_store

router = APIRouter(prefix="/local-accounts")
_COOKIE = "agent_t_local_account"
_COOKIE_PATH = "/api/local-accounts"


class Credentials(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class Bootstrap(Credentials):
    display_name: str = Field(min_length=1, max_length=100)


class NewAccount(Bootstrap):
    role: Literal["user", "admin"]


class ActiveChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    active: StrictBool


class RoleChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["user", "admin"]


class PasswordChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=1, max_length=256)


class PasswordReset(BaseModel):
    model_config = ConfigDict(extra="forbid")
    new_password: str = Field(min_length=1, max_length=256)
    admin_password: str = Field(min_length=1, max_length=256)


def _guard(request: Request, *, write: bool = False) -> Path:
    if os.getenv("ACCOUNT_LOCAL_ENABLED") != "1":
        raise HTTPException(404, "本机账户试验未开启")
    require_loopback(request, write=write)
    return _database_path()


def _database_path() -> Path:
    raw = os.getenv("ACCOUNT_DB_PATH", "").strip()
    if not raw or not Path(raw).is_absolute():
        raise HTTPException(503, "请配置独立的本机账户数据库")
    path = Path(raw).resolve()
    other_paths = [Path(review_store._path).resolve()]
    draft_path = os.getenv("RULE_DRAFTS_DB_PATH", "").strip()
    if draft_path:
        other_paths.append(Path(draft_path).resolve())
    for other in other_paths:
        if path == other or (path.exists() and other.exists() and os.path.samefile(path, other)):
            raise HTTPException(503, "账户数据库必须独立于审查和规则草稿数据库")
    return path


def _token(request: Request) -> str:
    return request.cookies.get(_COOKIE, "")


def _error(exc: Exception) -> HTTPException:
    if isinstance(exc, AuthenticationError):
        return HTTPException(401, "账号或密码无效，或会话已失效")
    if isinstance(exc, AuthorizationError):
        return HTTPException(403, str(exc))
    if isinstance(exc, ValueError):
        return HTTPException(422, str(exc))
    return HTTPException(500, "账户操作失败")


def _csrf(accounts: AccountStore, request: Request) -> str:
    token = _token(request)
    accounts.check_csrf(token, request.headers.get("x-csrf-token", ""))
    return token


@router.get("/capability")
def capability(request: Request):
    try:
        _guard(request)
    except HTTPException:
        return {"available": False, "protects_contracts": False}
    return {"available": True, "protects_contracts": False}


@router.post("/login")
def login(body: Credentials, request: Request):
    path = _guard(request, write=True)
    accounts = AccountStore(path)
    try:
        token = accounts.login(body.username, body.password)
        old = _token(request)
        if old:
            try:
                accounts.logout(old)
            except AuthenticationError:
                pass
        info = accounts.session_info(token)
    except (AuthenticationError, AuthorizationError, ValueError) as exc:
        raise _error(exc) from exc
    response = JSONResponse(info, headers={"Cache-Control": "no-store"})
    response.set_cookie(_COOKIE, token, httponly=True, samesite="strict", secure=False,
                        max_age=8 * 60 * 60, path=_COOKIE_PATH)
    return response


@router.get("/session")
def session(request: Request):
    path = _guard(request)
    try:
        info = AccountStore(path).session_info(_token(request))
    except (AuthenticationError, AuthorizationError, ValueError) as exc:
        raise _error(exc) from exc
    return JSONResponse(info, headers={"Cache-Control": "no-store"})


@router.post("/logout")
def logout(request: Request):
    path = _guard(request, write=True)
    accounts = AccountStore(path)
    try:
        token = _csrf(accounts, request)
        accounts.logout(token)
    except (AuthenticationError, AuthorizationError, ValueError) as exc:
        raise _error(exc) from exc
    response = JSONResponse({"ok": True}, headers={"Cache-Control": "no-store"})
    return response


@router.get("/users")
def users(request: Request):
    path = _guard(request)
    try:
        accounts = AccountStore(path)
        token = _csrf(accounts, request)
        return JSONResponse({"users": accounts.list_users(token)}, headers={"Cache-Control": "no-store"})
    except (AuthenticationError, AuthorizationError, ValueError) as exc:
        raise _error(exc) from exc


@router.post("/users")
def create_user(body: NewAccount, request: Request):
    path = _guard(request, write=True)
    accounts = AccountStore(path)
    try:
        token = _csrf(accounts, request)
        user = accounts.create_user(token, body.username, body.display_name, body.password, body.role)
        return JSONResponse(user, headers={"Cache-Control": "no-store"})
    except (AuthenticationError, AuthorizationError, ValueError) as exc:
        raise _error(exc) from exc


@router.post("/users/{user_id}/active")
def set_active(user_id: str, body: ActiveChange, request: Request):
    path = _guard(request, write=True)
    accounts = AccountStore(path)
    try:
        token = _csrf(accounts, request)
        accounts.set_active(token, user_id, body.active)
        return {"ok": True}
    except (AuthenticationError, AuthorizationError, ValueError) as exc:
        raise _error(exc) from exc


@router.post("/users/{user_id}/role")
def set_role(user_id: str, body: RoleChange, request: Request):
    path = _guard(request, write=True)
    accounts = AccountStore(path)
    try:
        token = _csrf(accounts, request)
        accounts.set_role(token, user_id, body.role)
        return {"ok": True}
    except (AuthenticationError, AuthorizationError, ValueError) as exc:
        raise _error(exc) from exc


@router.post("/password")
def change_password(body: PasswordChange, request: Request):
    path = _guard(request, write=True)
    accounts = AccountStore(path)
    try:
        token = _csrf(accounts, request)
        accounts.change_password(token, body.current_password, body.new_password)
    except (AuthenticationError, AuthorizationError, ValueError) as exc:
        raise _error(exc) from exc
    response = JSONResponse({"ok": True}, headers={"Cache-Control": "no-store"})
    return response


@router.post("/users/{user_id}/password")
def reset_password(user_id: str, body: PasswordReset, request: Request):
    path = _guard(request, write=True)
    accounts = AccountStore(path)
    try:
        token = _csrf(accounts, request)
        accounts.reset_password(token, user_id, body.new_password, body.admin_password)
    except (AuthenticationError, AuthorizationError, ValueError) as exc:
        raise _error(exc) from exc
    return JSONResponse({"ok": True}, headers={"Cache-Control": "no-store"})
