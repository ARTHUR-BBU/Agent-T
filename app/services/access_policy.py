"""Deny-by-default authorization primitives, not yet wired to application routes.

Callers must load review/candidate ownership from trusted server-side storage.
"""
from __future__ import annotations

from typing import Any, Mapping

from app.services.account_store import AccountStore, AuthorizationError


def require_review_owner(accounts: AccountStore, token: str, review: Mapping[str, Any]) -> dict[str, Any]:
    """No implicit administrator override and no assignment of legacy unowned records."""
    actor = accounts.authenticate(token)
    owner = review.get("owner_user_id")
    if not isinstance(owner, str) or not owner or owner != actor["id"]:
        raise AuthorizationError("无权访问该审查")
    return actor


def require_rule_action(accounts: AccountStore, token: str, action: str, *,
                        submitted_by: str | None = None) -> dict[str, Any]:
    """Return authenticated identity; user-authorized admin self-review is marked."""
    if action not in {"submit", "review", "publish", "rollback"}:
        raise AuthorizationError("未知操作")
    actor = accounts.authenticate(token, admin=action != "submit")
    if action == "review":
        if not isinstance(submitted_by, str) or not submitted_by:
            raise AuthorizationError("审核需要可信的提交人")
        return {**actor, "self_review": actor["id"] == submitted_by}
    return actor
