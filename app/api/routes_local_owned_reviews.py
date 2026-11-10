"""Read-only, isolated account-owned review experiment; not application-wide auth."""
from __future__ import annotations

import hmac
import json
import os
import re
import sqlite3
import time
from contextlib import closing

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from app.api.routes_local_accounts import _error, _guard, _token
from app.services.account_store import AccountStore, AuthenticationError, AuthorizationError, _token_digest
from app.services.store import store as review_store

router = APIRouter(prefix="/local-accounts/contracts")
_DETAIL_FIELDS = ("filename", "category", "category_label", "status", "stage", "stance", "items",
                  "scorecard", "text_preview", "rule_provenance", "completion")
_PRECHECK_REASONS = frozenset({"disabled", "no_llm_key", "llm_error", "parse_failed", "budget_exceeded", "busy", "category_confirmation"})


def _precheck_view(data):
    value = data.get("precheck")
    if not isinstance(value, dict):
        return None
    reason = value.get("skip_reason")
    if reason is not None and (not isinstance(reason, str) or reason not in _PRECHECK_REASONS):
        reason = "unknown"
    result = {key: value.get(key) if type(value.get(key)) is bool else None
              for key in ("performed", "suggestion_performed", "suspect")}
    result["skip_reason"] = reason
    for key in ("summary", "original_category", "original_stance", "selected_category", "selected_stance"):
        field_value = value.get(key)
        result[key] = field_value[:300] if isinstance(field_value, str) else None
    return result


def _read(request: Request, *, review_id: str | None = None, limit: int = 50):
    if os.getenv("CONTRACT_ACCOUNT_READ_TRIAL") != "1":
        raise HTTPException(404, "账户合同试验未开启")
    try:
        path = _guard(request)
        if not path.is_file():
            raise HTTPException(503, "账户合同试验数据库不可用")
    except OSError as exc:
        raise HTTPException(503, "账户合同试验数据库不可用") from exc
    token = _token(request)
    csrf = request.headers.get("x-csrf-token", "")
    try:
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=10)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            conn.execute("PRAGMA trusted_schema=OFF")
            conn.execute("BEGIN")
            actor = AccountStore._session(conn, token)
            session = conn.execute("SELECT csrf_token FROM account_sessions WHERE token_hash=?", (_token_digest(token),)).fetchone()
            if (not session or not re.fullmatch(r"[A-Za-z0-9_-]{43}", csrf)
                    or not hmac.compare_digest(csrf, session["csrf_token"])):
                raise AuthorizationError("请求验证失败")
            ttl = review_store.ttl_seconds
            cutoff = time.time() - ttl if ttl > 0 else None
            if review_id is None:
                rows = conn.execute("SELECT id,created_at,data FROM reviews WHERE owner_user_id=? "
                                    "AND (? IS NULL OR created_at>=?) ORDER BY created_at DESC,id DESC LIMIT ?",
                                    (actor["id"], cutoff, cutoff, limit)).fetchall()
                result = []
                for row in rows:
                    data = json.loads(row["data"])
                    result.append({"id": row["id"], "created_at": row["created_at"],
                                   **{key: data.get(key) for key in ("filename", "category", "category_label", "status")}})
                return {"reviews": result, "scope": "isolated_read_trial"}
            row = conn.execute("SELECT id,data FROM reviews WHERE id=? AND owner_user_id=? "
                               "AND (? IS NULL OR created_at>=?)", (review_id, actor["id"], cutoff, cutoff)).fetchone()
            if row is None:
                raise HTTPException(404, "审查记录不存在")
            data = json.loads(row["data"])
            return {"id": row["id"], **{key: data.get(key) for key in _DETAIL_FIELDS},
                    "precheck": _precheck_view(data), "scope": "isolated_read_trial"}
    except (AuthenticationError, AuthorizationError) as exc:
        raise _error(exc) from exc
    except (sqlite3.Error, OSError, ValueError, TypeError, AttributeError) as exc:
        raise HTTPException(503, "账户合同试验暂不可用") from exc


@router.get("")
def list_owned_reviews(request: Request, limit: int = Query(default=50, ge=1, le=50)):
    return JSONResponse(_read(request, limit=limit), headers={"Cache-Control": "no-store"})


@router.get("/{review_id}")
def get_owned_review(review_id: str, request: Request):
    return JSONResponse(_read(request, review_id=review_id), headers={"Cache-Control": "no-store"})
