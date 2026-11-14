"""Opt-in local account upload experiment; not a production access mode."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
import re
import secrets
import threading
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from starlette.datastructures import UploadFile

from app.api.routes_local_accounts import _csrf, _error, _guard
from app.services import owned_llm_transport, rate_limit, stance, store as store_module
from app.services.account_store import AccountStore, AuthenticationError, AuthorizationError, _token_digest
from app.services.owned_llm_ledger import OwnedLLMLedger
from app.services.owned_review_jobs import OwnedReviewJobs
from app.services.owned_review_precheck import confirm_account_intake, prepare_account_intake
from app.services.owned_review_store import OwnedReviewStore
from app.services.owned_review_worker import OwnedReviewWorker
from app.services.rule_execution_trial import read_trial_versions
from app.services.store import ReviewStore

router = APIRouter(prefix="/local-accounts/contracts")
logger = logging.getLogger(__name__)
_SLOTS = threading.BoundedSemaphore(4)
_LOCK = threading.Lock()
_PENDING: dict[str, tuple[float, str, str, str, str, str, str]] = {}
_PENDING_BUSY: set[str] = set()
_MAX_PENDING = 64
_TTL = 300
_SUFFIXES = {".txt", ".md", ".text", ".pdf", ".docx", ".doc"}
_USER_LIMITER = rate_limit.SlidingWindowLimiter("RATE_LIMIT_UPLOAD_PER_MINUTE", 10)


def _prune(now: float) -> None:
    for key, value in list(_PENDING.items()):
        if value[0] <= now and key not in _PENDING_BUSY:
            del _PENDING[key]


def _issue(token: str, job: str, secret: str, rid: str, filename: str, kind: str) -> str:
    ticket = secrets.token_urlsafe(32)
    key = hashlib.sha256(ticket.encode("ascii")).hexdigest()
    with _LOCK:
        now = time.monotonic()
        _prune(now)
        if len(_PENDING) >= _MAX_PENDING:
            raise RuntimeError("等待确认的任务过多，请稍后重试")
        _PENDING[key] = (now + _TTL, _token_digest(token), job, secret, rid, filename, kind)
    return ticket


def _confirm(jobs: OwnedReviewJobs, token: str, ticket: str, *, raw: bytes, filename: str,
             kind: str, category: str) -> tuple[str, str, str, str]:
    if not isinstance(ticket, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", ticket):
        raise AuthorizationError("确认已失效，请重新提交")
    key = hashlib.sha256(ticket.encode("ascii")).hexdigest()
    with _LOCK:
        _prune(time.monotonic())
        pending = _PENDING.get(key)
        if (not pending or key in _PENDING_BUSY or not hmac.compare_digest(pending[1], _token_digest(token))
                or pending[5] != filename or pending[6] != kind):
            raise AuthorizationError("确认已失效，请重新提交")
        _PENDING_BUSY.add(key)
        _, _, job, secret, rid, _, _ = pending
    try:
        selected_stance = confirm_account_intake(jobs, token, job, secret, raw=raw, input_kind=kind, category=category)
        with _LOCK:
            _PENDING.pop(key, None)
        return job, secret, rid, selected_stance
    finally:
        with _LOCK:
            _PENDING_BUSY.discard(key)


def _authenticate(request: Request) -> tuple[AccountStore, str, Path, str]:
    path = _guard(request, write=True)
    if not path.is_file():
        raise HTTPException(503, "账户数据库不可用")
    accounts = AccountStore(path)
    token = _csrf(accounts, request)
    actor = accounts.authenticate(token)
    return accounts, token, path, actor["id"]


def _services(accounts: AccountStore, path: Path) -> tuple[OwnedReviewJobs, OwnedLLMLedger, OwnedReviewWorker]:
    jobs = OwnedReviewJobs(OwnedReviewStore(accounts, ReviewStore(str(path))))
    ledger = OwnedLLMLedger(jobs)
    return jobs, ledger, OwnedReviewWorker(jobs, ledger)


async def _input(request: Request) -> tuple[bytes, str, str, str, str, str]:
    try:
        async with request.form(max_files=1, max_fields=8, max_part_size=1024 * 1024) as form:
            entries = list(form.multi_items())
            keys = [key for key, _ in entries]
            if len(keys) != len(set(keys)) or set(keys) - {"file", "pasted_text", "category", "stance", "confirmation_token"}:
                raise HTTPException(422, "上传字段无效")
            file, text = form.get("file"), form.get("pasted_text", "")
            category, selected_stance, ticket = form.get("category", ""), form.get("stance", ""), form.get("confirmation_token", "")
            if not all(isinstance(value, str) for value in (text, category, selected_stance, ticket)):
                raise HTTPException(422, "上传字段无效")
            if not re.fullmatch(r"[a-z0-9_]{1,64}", category) or len(selected_stance) > 64 or len(ticket) > 128:
                raise HTTPException(422, "合同类型或确认信息无效")
            if file is not None and not isinstance(file, UploadFile):
                raise HTTPException(422, "上传文件无效")
            has_file = isinstance(file, UploadFile) and bool(file.filename)
            if has_file == bool(text.strip()):
                raise HTTPException(400, "请上传文件或粘贴合同文本，二者只能选择一项")
            if has_file:
                filename = (file.filename or "").replace("\\", "/").split("/")[-1]
                if (not filename or len(filename) > 255 or any(ord(c) < 32 for c in filename)
                        or Path(filename).suffix.lower() not in _SUFFIXES):
                    raise HTTPException(400, "不支持的文件类型或文件名")
                chunks, total = [], 0
                while True:
                    chunk = await file.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > 10 * 1024 * 1024:
                        raise HTTPException(413, "文件超过 10MB 上限")
                    chunks.append(chunk)
                raw, kind = b"".join(chunks), "file"
            else:
                text = text.strip()
                if len(text) > 200_000:
                    raise HTTPException(413, "超出 20 万字符上限")
                raw, filename, kind = text.encode("utf-8"), "粘贴合同.txt", "pasted_text"
            if not raw:
                raise HTTPException(400, "合同内容为空")
            return raw, filename, kind, category, selected_stance, ticket
    except HTTPException:
        raise
    except (ValueError, TypeError) as exc:
        raise HTTPException(400, "上传内容无效") from exc


def _fail(jobs: OwnedReviewJobs, token: str, job: str, secret: str) -> None:
    try:
        jobs.commit(token, job, secret, result={"error": "审查任务未完成，请重新提交"}, failed=True)
    except Exception:
        pass  # Revoked or expired jobs are handled by the explicit reaper.


def _start(runner: OwnedReviewWorker, jobs: OwnedReviewJobs, token: str, job: str, secret: str,
           raw: bytes, selected_stance: str) -> None:
    def run() -> None:
        try:
            runner.run(token, job, secret, raw=raw, declared_stance=selected_stance)
        except Exception as exc:
            _fail(jobs, token, job, secret)
            logger.error("账户审查任务失败：%s", type(exc).__name__)
        finally:
            _SLOTS.release()

    threading.Thread(target=run, daemon=True).start()


class _RequestAbandoned(Exception):
    pass


def _workflow(accounts: AccountStore, token: str, path: Path, payload: tuple[bytes, str, str, str, str, str],
              cancelled: threading.Event) -> dict:
    raw, filename, kind, category, selected_stance, ticket = payload
    transferred = False
    job = secret = None
    jobs = None
    try:
        if cancelled.is_set():
            raise _RequestAbandoned()
        jobs, ledger, runner = _services(accounts, path)
        if cancelled.is_set():
            raise _RequestAbandoned()
        if ticket:
            if selected_stance:
                raise HTTPException(422, "确认时立场由服务器根据原选择确定")
            job, secret, rid, selected_stance = _confirm(jobs, token, ticket, raw=raw, filename=filename,
                                                         kind=kind, category=category)
        else:
            versions = read_trial_versions(path)
            selected = versions.get(category)
            if selected is None or selected.snapshot.configuration().get("hidden"):
                raise HTTPException(422, "合同类型不可用")
            if not selected_stance or not stance.is_allowed(category, selected_stance, configuration=selected.snapshot.configuration()):
                raise HTTPException(422, "合同立场无效")
            if cancelled.is_set():
                raise _RequestAbandoned()
            rid, job, secret = jobs.create(token, filename=filename, category=category, raw=raw,
                                           declared_stance=selected_stance, input_kind=kind, require_precheck=True)
            if cancelled.is_set():
                raise _RequestAbandoned()
            intake = prepare_account_intake(jobs, ledger, token, job, secret, raw=raw, declared_stance=selected_stance)
            if cancelled.is_set():
                raise _RequestAbandoned()
            if intake.action == "confirm":
                confirmation = _issue(token, job, secret, rid, filename, kind)
                return {"status": "category_confirm", "review_id": rid, "suggested_category": intake.suggested_category,
                        "summary": intake.summary, "confirmation_token": confirmation,
                        "precheck_performed": intake.precheck_performed, "precheck_skip_reason": intake.precheck_skip_reason}
        if cancelled.is_set():
            raise _RequestAbandoned()
        _start(runner, jobs, token, job, secret, raw, selected_stance)
        transferred = True
        return {"status": "uploaded", "review_id": rid}
    except _RequestAbandoned:
        if jobs is not None and job and secret:
            _fail(jobs, token, job, secret)
        return {"status": "cancelled"}
    except HTTPException:
        if jobs is not None and job and secret:
            _fail(jobs, token, job, secret)
        raise
    except (AuthenticationError, AuthorizationError, ValueError) as exc:
        if jobs is not None and job and secret:
            _fail(jobs, token, job, secret)
        raise _error(exc) from exc
    except Exception as exc:
        if jobs is not None and job and secret:
            _fail(jobs, token, job, secret)
        logger.error("账户上传试验失败：%s", type(exc).__name__)
        raise HTTPException(503, "账户上传暂不可用，请重新提交") from exc
    finally:
        if not transferred:
            _SLOTS.release()


@router.post("/upload")
async def upload_owned_contract(request: Request):
    if (not owned_llm_transport.enabled() or store_module._ACCOUNT_MODE != "1"
            or os.getenv("CONTRACT_ACCOUNT_UPLOAD_TRIAL") != "1"
            or os.getenv("CONTRACT_ACCOUNT_READ_TRIAL") != "1"
            or os.getenv("RULE_EXECUTION_DB_TRIAL") != "1"):
        raise HTTPException(404, "账户上传试验未开启")
    try:
        accounts, token, path, actor_id = await run_in_threadpool(_authenticate, request)
    except (AuthenticationError, AuthorizationError, ValueError) as exc:
        raise _error(exc) from exc
    await rate_limit.upload_rate_limit(request)
    if _USER_LIMITER.limit() > 0 and not _USER_LIMITER.allow(actor_id):
        raise HTTPException(429, "上传过于频繁，请稍后重试", headers={"Retry-After": "60"})
    if not _SLOTS.acquire(blocking=False):
        raise HTTPException(429, "当前审查排队已满，请稍后重试")
    transferred = False
    try:
        payload = await _input(request)
        cancelled = threading.Event()
        task = asyncio.create_task(run_in_threadpool(_workflow, accounts, token, path, payload, cancelled))
        transferred = True
        try:
            result = await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled.set()
            task.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)
            raise
        return JSONResponse(result, headers={"Cache-Control": "no-store"})
    finally:
        if not transferred:
            _SLOTS.release()
