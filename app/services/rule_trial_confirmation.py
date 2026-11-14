"""Short-lived, single-use confirmation capabilities for the local rule trial.

Process-local by design: a restart or another worker invalidates outstanding
tickets. This does not authenticate users or protect shared contract records.
"""
from __future__ import annotations

import hashlib
import secrets
import threading
import time

_LOCK = threading.Lock()
_TICKETS: dict[str, tuple[float, str, str, dict[str, tuple[str, int, str, str]]]] = {}
_TTL_SECONDS = 300
_MAX_TICKETS = 1000


def _prune(now: float) -> None:
    for key, (expires, *_rest) in list(_TICKETS.items()):
        if expires <= now:
            del _TICKETS[key]


def issue(document_sha256: str, input_kind: str, choices: dict[str, tuple[str, int, str, str]]) -> str:
    if not choices:
        raise ValueError("确认选项为空")
    token = secrets.token_urlsafe(32)
    digest = hashlib.sha256(token.encode("ascii")).hexdigest()
    now = time.monotonic()
    with _LOCK:
        _prune(now)
        if len(_TICKETS) >= _MAX_TICKETS:
            raise RuntimeError("确认请求过多，请稍后重试")
        _TICKETS[digest] = (now + _TTL_SECONDS, document_sha256, input_kind, dict(choices))
    return token


def consume(token: str, *, document_sha256: str, input_kind: str, category: str,
            selected_version: tuple[str, int, str, str]) -> bool:
    if not isinstance(token, str) or len(token) > 128 or not token.isascii():
        return False
    digest = hashlib.sha256(token.encode("ascii")).hexdigest()
    with _LOCK:
        now = time.monotonic()
        _prune(now)
        ticket = _TICKETS.pop(digest, None)
        if ticket is None:
            return False
        expires, document, kind, choices = ticket
        return (expires > now and secrets.compare_digest(document, document_sha256) and
                kind == input_kind and choices.get(category) == selected_version)
