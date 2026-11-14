"""Permission rules are server-side foundations; application routes are not yet wired."""
from __future__ import annotations

import pytest

from app.services.account_store import AccountStore, AuthorizationError
from app.services.access_policy import require_review_owner, require_rule_action
from tests.helpers import create_active_account


def test_contracts_are_private_and_legacy_unowned_records_deny_by_default(tmp_path):
    accounts = AccountStore(tmp_path / "accounts.sqlite")
    accounts.bootstrap_admin("admin", "管理员", "testing-password-123")
    admin = accounts.login("admin", "testing-password-123")
    alice = create_active_account(accounts, admin, "alice", "甲", "alice-password-123", "user")
    create_active_account(accounts, admin, "bob", "乙", "bobby-password-123", "user")
    alice_token = accounts.login("alice", "alice-password-123")
    bob_token = accounts.login("bob", "bobby-password-123")
    row = {"id": "review-1", "owner_user_id": alice["id"]}
    assert require_review_owner(accounts, alice_token, row)["id"] == alice["id"]
    for token in (bob_token, admin):
        with pytest.raises(AuthorizationError):
            require_review_owner(accounts, token, row)
    with pytest.raises(AuthorizationError):
        require_review_owner(accounts, alice_token, {"id": "legacy"})


def test_only_admin_reviews_and_admin_self_review_is_identified(tmp_path):
    accounts = AccountStore(tmp_path / "accounts.sqlite")
    admin = accounts.bootstrap_admin("admin", "管理员", "testing-password-123")
    admin_token = accounts.login("admin", "testing-password-123")
    user = create_active_account(accounts, admin_token, "alice", "甲", "alice-password-123", "user")
    user_token = accounts.login("alice", "alice-password-123")
    assert require_rule_action(accounts, user_token, "submit")["id"] == user["id"]
    with pytest.raises(AuthorizationError):
        require_rule_action(accounts, user_token, "review", submitted_by=user["id"])
    review = require_rule_action(accounts, admin_token, "review", submitted_by=admin["id"])
    assert review["id"] == admin["id"] and review["self_review"] is True
    assert require_rule_action(accounts, admin_token, "review", submitted_by=user["id"])["self_review"] is False
    with pytest.raises(AuthorizationError):
        require_rule_action(accounts, admin_token, "unexpected")
