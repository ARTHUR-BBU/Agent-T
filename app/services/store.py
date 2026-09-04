"""In-memory review store (MVP)."""
from __future__ import annotations

import threading
import uuid
from typing import Any


class ReviewStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: dict[str, dict[str, Any]] = {}

    def create(self, **kwargs: Any) -> str:
        rid = uuid.uuid4().hex[:12]
        with self._lock:
            self._data[rid] = {"id": rid, **kwargs}
        return rid

    def get(self, review_id: str) -> dict[str, Any] | None:
        with self._lock:
            return self._data.get(review_id)

    def update(self, review_id: str, **kwargs: Any) -> dict[str, Any] | None:
        with self._lock:
            row = self._data.get(review_id)
            if not row:
                return None
            row.update(kwargs)
            return row


store = ReviewStore()
