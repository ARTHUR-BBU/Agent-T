"""Cancellation must not release capacity while synchronous work is still running."""
import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.api import routes_local_owned_upload as upload
from app.services import owned_llm_transport, store as store_module


def test_cancel_during_precheck_retains_slot_and_marks_job_failed(monkeypatch):
    entered, release, failed = threading.Event(), threading.Event(), threading.Event()
    semaphore = threading.BoundedSemaphore(1)
    monkeypatch.setattr(upload, "_SLOTS", semaphore)
    monkeypatch.setattr(owned_llm_transport, "_PROCESS_MODE", "1")
    monkeypatch.setattr(store_module, "_ACCOUNT_MODE", "1")
    for key in ("CONTRACT_ACCOUNT_UPLOAD_TRIAL", "CONTRACT_ACCOUNT_READ_TRIAL", "RULE_EXECUTION_DB_TRIAL"):
        monkeypatch.setenv(key, "1")
    monkeypatch.setattr(upload, "_authenticate", lambda request: (object(), "token", Path("fake"), "actor"))
    monkeypatch.setattr(upload._USER_LIMITER, "limit", lambda: 0)

    async def no_rate_limit(request):
        return None

    async def fake_input(request):
        return b"fictional", "fiction.txt", "file", "procurement", "neutral", ""

    class Jobs:
        def create(self, *args, **kwargs):
            return "review", "job", "secret"

    def prepare(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return SimpleNamespace(action="proceed")

    monkeypatch.setattr(upload.rate_limit, "upload_rate_limit", no_rate_limit)
    monkeypatch.setattr(upload, "_input", fake_input)
    monkeypatch.setattr(upload, "_services", lambda *args: (Jobs(), object(), object()))
    monkeypatch.setattr(upload, "read_trial_versions", lambda path: {"procurement": SimpleNamespace(
        snapshot=SimpleNamespace(configuration=lambda: {}))})
    monkeypatch.setattr(upload.stance, "is_allowed", lambda *args, **kwargs: True)
    monkeypatch.setattr(upload, "prepare_account_intake", prepare)
    monkeypatch.setattr(upload, "_fail", lambda *args: failed.set())
    monkeypatch.setattr(upload, "_start", lambda *args: pytest.fail("cancelled job must not start"))

    async def exercise():
        task = asyncio.create_task(upload.upload_owned_contract(object()))
        assert await asyncio.to_thread(entered.wait, 10)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not semaphore.acquire(blocking=False)
        release.set()
        assert await asyncio.to_thread(failed.wait, 10)
        for _ in range(100):
            if semaphore.acquire(blocking=False):
                semaphore.release()
                return
            await asyncio.sleep(0.01)
        pytest.fail("capacity was not released after background work ended")

    try:
        asyncio.run(exercise())
    finally:
        release.set()


def test_thread_start_failure_marks_task_failed_and_releases_slot(monkeypatch):
    from fastapi import HTTPException

    semaphore = threading.BoundedSemaphore(1)
    assert semaphore.acquire(blocking=False)
    monkeypatch.setattr(upload, "_SLOTS", semaphore)
    failed = []

    class Jobs:
        def create(self, *args, **kwargs):
            return "review", "job", "secret"

    def cannot_start(*args):
        raise RuntimeError("synthetic thread failure")

    monkeypatch.setattr(upload, "_services", lambda *args: (Jobs(), object(), object()))
    monkeypatch.setattr(upload, "read_trial_versions", lambda path: {"procurement": SimpleNamespace(
        snapshot=SimpleNamespace(configuration=lambda: {}))})
    monkeypatch.setattr(upload.stance, "is_allowed", lambda *args, **kwargs: True)
    monkeypatch.setattr(upload, "prepare_account_intake", lambda *args, **kwargs: SimpleNamespace(action="proceed"))
    monkeypatch.setattr(upload, "_fail", lambda jobs, token, job, secret: failed.append(job))
    monkeypatch.setattr(upload, "_start", cannot_start)
    with pytest.raises(HTTPException) as exc:
        upload._workflow(object(), "token", Path("fake"),
                         (b"fictional", "fiction.txt", "file", "procurement", "neutral", ""), threading.Event())
    assert exc.value.status_code == 503 and failed == ["job"]
    assert semaphore.acquire(blocking=False)
    semaphore.release()


def test_cancel_during_job_creation_does_not_run_precheck(monkeypatch):
    entered, release, failed = threading.Event(), threading.Event(), threading.Event()
    semaphore = threading.BoundedSemaphore(1)
    monkeypatch.setattr(upload, "_SLOTS", semaphore)
    monkeypatch.setattr(owned_llm_transport, "_PROCESS_MODE", "1")
    monkeypatch.setattr(store_module, "_ACCOUNT_MODE", "1")
    for key in ("CONTRACT_ACCOUNT_UPLOAD_TRIAL", "CONTRACT_ACCOUNT_READ_TRIAL", "RULE_EXECUTION_DB_TRIAL"):
        monkeypatch.setenv(key, "1")
    monkeypatch.setattr(upload, "_authenticate", lambda request: (object(), "token", Path("fake"), "actor"))
    monkeypatch.setattr(upload._USER_LIMITER, "limit", lambda: 0)

    async def no_rate_limit(request):
        return None

    async def fake_input(request):
        return b"fictional", "fiction.txt", "file", "procurement", "neutral", ""

    class Jobs:
        def create(self, *args, **kwargs):
            entered.set()
            assert release.wait(10)
            return "review", "job", "secret"

    monkeypatch.setattr(upload.rate_limit, "upload_rate_limit", no_rate_limit)
    monkeypatch.setattr(upload, "_input", fake_input)
    monkeypatch.setattr(upload, "_services", lambda *args: (Jobs(), object(), object()))
    monkeypatch.setattr(upload, "read_trial_versions", lambda path: {"procurement": SimpleNamespace(
        snapshot=SimpleNamespace(configuration=lambda: {}))})
    monkeypatch.setattr(upload.stance, "is_allowed", lambda *args, **kwargs: True)
    monkeypatch.setattr(upload, "prepare_account_intake", lambda *args, **kwargs: pytest.fail("precheck must not start"))
    monkeypatch.setattr(upload, "_fail", lambda *args: failed.set())

    async def exercise():
        task = asyncio.create_task(upload.upload_owned_contract(object()))
        assert await asyncio.to_thread(entered.wait, 10)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not semaphore.acquire(blocking=False)
        release.set()
        assert await asyncio.to_thread(failed.wait, 10)
        for _ in range(100):
            if semaphore.acquire(blocking=False):
                semaphore.release()
                return
            await asyncio.sleep(0.01)
        pytest.fail("capacity was not released after job creation ended")

    try:
        asyncio.run(exercise())
    finally:
        release.set()
