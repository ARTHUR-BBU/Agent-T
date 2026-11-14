"""Account-bound local rule review experiment; it cannot publish rules."""
from __future__ import annotations

import hashlib
import os
import re
import sqlite3
from contextlib import closing
from typing import Literal

import yaml
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from app.api.routes_local_accounts import _csrf, _error, _guard
from app.api.routes_rule_drafts import DraftInput, _hardline
from app.services.account_store import AccountStore, AuthenticationError, AuthorizationError
from app.services.checklist import list_categories
from app.services.rule_review_ledger import RuleReviewLedger
from app.services.rule_snapshot import RuleSnapshot, capture_rule_snapshot
from app.services.rule_submission import RuleSubmissionService
from app.services.rule_validation_service import RuleValidationService
from app.services.rule_versions import RuleVersionStore
from app.services.rule_validation_guard import validation_state
from app.services.rule_release import RuleReleaseService
from app.services import owned_review_maintenance

router = APIRouter(prefix="/local-accounts/rules")


class Initialize(BaseModel):
    model_config = ConfigDict(extra="forbid")
    category: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9_]+$")


class CandidateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    draft: DraftInput
    expected_generation: int = Field(ge=1, strict=True)
    request_id: str = Field(min_length=16, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    examples_acknowledged: StrictBool


class ReviewInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    candidate_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    decision: Literal["approve", "reject"]
    reason: str = Field(min_length=1, max_length=1000)
    expected_validation_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")


class ReleaseInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    candidate_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    expected_generation: int = Field(ge=1, strict=True)
    validation_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    review_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    full_result_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    request_id: str = Field(min_length=16, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")


class RollbackInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    category: str = Field(pattern=r"^[a-z0-9_]{1,64}$")
    version_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    expected_generation: int = Field(ge=1, strict=True)
    reason: str = Field(min_length=1, max_length=1000)
    request_id: str = Field(min_length=16, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")


def _accounts(request: Request, *, write: bool = False) -> tuple[AccountStore, str]:
    accounts = AccountStore(_guard(request, write=write))
    try:
        token = _csrf(accounts, request)
        accounts.authenticate(token)
        return accounts, token
    except (AuthenticationError, AuthorizationError, ValueError) as exc:
        raise _error(exc) from exc


def _failure(exc: Exception) -> HTTPException:
    if isinstance(exc, (AuthenticationError, AuthorizationError, ValueError)):
        return _error(exc)
    if isinstance(exc, RuntimeError):
        return HTTPException(409, str(exc))
    return HTTPException(503, "规则管理暂不可用")


def _response(body):
    return JSONResponse(body, headers={"Cache-Control": "no-store"})


def _release_accounts(request: Request, *, write: bool = False) -> tuple[AccountStore, str]:
    if os.getenv("RULE_RELEASE_LOCAL_TRIAL") != "1" or not owned_review_maintenance.enabled():
        raise HTTPException(404, "规则发布试验未开启")
    accounts, token = _accounts(request, write=write)
    try:
        accounts.authenticate(token, admin=True)
    except (AuthenticationError, AuthorizationError, ValueError) as exc:
        raise _error(exc) from exc
    return accounts, token


@router.get("/release-history")
def release_history(request: Request):
    accounts, token = _release_accounts(request)
    try:
        RuleReleaseService(accounts)
        with closing(accounts._connect()) as conn:
            conn.execute("BEGIN")
            accounts._session(conn, token, admin=True)
            active = conn.execute("SELECT category,version_id,generation FROM rule_active_versions ORDER BY category").fetchall()
            events = conn.execute("SELECT e.id,e.category,e.action,e.previous_version_id,e.version_id,e.previous_generation,"
                                  "e.generation,e.reason,e.created_at,u.username,u.display_name FROM rule_release_events e "
                                  "JOIN account_users u ON u.id=e.actor_id ORDER BY e.rowid DESC LIMIT 101").fetchall()
            versions = conn.execute("SELECT v.id,v.category,v.sha256 FROM rule_versions v WHERE EXISTS "
                                    "(SELECT 1 FROM rule_release_events e WHERE e.category=v.category AND "
                                    "(e.version_id=v.id OR e.previous_version_id=v.id)) ORDER BY v.created_at DESC LIMIT 101").fetchall()
            return _response({"scope": "local_test", "active": [dict(row) for row in active],
                              "events": [dict(row) for row in events[:100]], "versions": [dict(row) for row in versions[:100]],
                              "events_truncated": len(events) > 100, "versions_truncated": len(versions) > 100})
    except (AuthenticationError, AuthorizationError, ValueError, RuntimeError, OSError, sqlite3.Error, yaml.YAMLError) as exc:
        raise _failure(exc) from exc


@router.post("/candidates/{candidate_id}/publish")
def publish_candidate(candidate_id: str, body: ReleaseInput, request: Request):
    accounts, token = _release_accounts(request, write=True)
    try:
        if not re.fullmatch(r"[a-f0-9]{32}", candidate_id):
            raise ValueError("候选规则编号无效")
        return _response(RuleReleaseService(accounts).publish(
            token, candidate_id, expected_generation=body.expected_generation, validation_id=body.validation_id,
            review_id=body.review_id, full_result_id=body.full_result_id, request_id=body.request_id,
            expected_candidate_sha256=body.candidate_sha256))
    except (AuthenticationError, AuthorizationError, ValueError, RuntimeError, OSError, sqlite3.Error, yaml.YAMLError) as exc:
        raise _failure(exc) from exc


@router.post("/rollback")
def rollback(body: RollbackInput, request: Request):
    accounts, token = _release_accounts(request, write=True)
    try:
        return _response(RuleReleaseService(accounts).rollback(
            token, body.category, body.version_id, expected_generation=body.expected_generation,
            reason=body.reason, request_id=body.request_id))
    except (AuthenticationError, AuthorizationError, ValueError, RuntimeError, OSError, sqlite3.Error, yaml.YAMLError) as exc:
        raise _failure(exc) from exc


@router.post("/initialize")
def initialize(body: Initialize, request: Request):
    accounts, token = _accounts(request, write=True)
    try:
        accounts.authenticate(token, admin=True)
        if body.category not in {item["id"] for item in list_categories()}:
            raise ValueError("请选择已开放的合同类型")
        snapshot = capture_rule_snapshot(body.category)
        version = RuleVersionStore(accounts).initialize(token, body.category, snapshot.config_bytes)
        return _response({**version, "generation": 1})
    except (AuthenticationError, AuthorizationError, ValueError, RuntimeError, OSError, sqlite3.Error, yaml.YAMLError) as exc:
        raise _failure(exc) from exc


@router.get("/versions")
def versions(request: Request):
    accounts, token = _accounts(request)
    try:
        RuleVersionStore(accounts)
        RuleValidationService(accounts)
        with closing(accounts._connect()) as conn:
            conn.execute("BEGIN")
            accounts._session(conn, token)
            state = validation_state(conn)
            rows = conn.execute("SELECT v.*, a.generation FROM rule_active_versions a "
                                "JOIN rule_versions v ON v.id=a.version_id AND v.category=a.category "
                                "ORDER BY a.category").fetchall()
            result = []
            for row in rows:
                digest = hashlib.sha256(row["content"]).hexdigest()
                if digest != row["sha256"]:
                    raise ValueError("规则版本内容损坏")
                config = RuleSnapshot(row["category"], row["content"], digest[:12]).configuration()
                result.append({"id": row["id"], "category": row["category"], "label": config.get("label", row["category"]),
                               "sha256": digest, "generation": row["generation"],
                               "items": [{"id": item["id"], "name": item["name"],
                                          "description": item.get("description", ""),
                                          "enabled": item.get("enabled", True), "has_hardline": _hardline(item)}
                                         for item in config["items"]],
                               "segments": [{"key": segment.get("key"), "name": segment.get("name")}
                                            for segment in (config.get("scorecard") or {}).get("segments", [])]})
        trial_execution = os.getenv("RULE_EXECUTION_DB_TRIAL") == "1"
        return _response({"versions": result, "effective_in_reviews": trial_execution,
                          "execution_scope": "local_test" if trial_execution else "not_connected",
                          "validation_blocked": state != "ready", "validation_state": state,
                          "release_trial_available": os.getenv("RULE_RELEASE_LOCAL_TRIAL") == "1" and owned_review_maintenance.enabled()})
    except (AuthenticationError, AuthorizationError, ValueError, RuntimeError, OSError, sqlite3.Error, yaml.YAMLError) as exc:
        raise _failure(exc) from exc


@router.post("/candidates")
def submit(body: CandidateInput, request: Request):
    accounts, token = _accounts(request, write=True)
    if not body.examples_acknowledged:
        raise HTTPException(422, "请确认示例可供管理员查看并保存在本机")
    if body.draft.revision is not None:
        raise HTTPException(422, "此接口提交新候选，不接收历史草稿版本")
    try:
        result = RuleSubmissionService(accounts).submit(token, body.draft.model_dump(exclude={"revision"}),
                                                        expected_generation=body.expected_generation,
                                                        request_id=body.request_id)
        return _response(result)
    except (AuthenticationError, AuthorizationError, ValueError, RuntimeError, OSError, sqlite3.Error, yaml.YAMLError) as exc:
        raise _failure(exc) from exc


@router.get("/candidates")
def candidates(request: Request, limit: int = Query(default=50, ge=1, le=100),
               before: str | None = Query(default=None, pattern=r"^[a-f0-9]{32}$")):
    accounts, token = _accounts(request)
    try:
        RuleValidationService(accounts)
        ledger = RuleReviewLedger(accounts, versioned=True)
        with closing(accounts._connect()) as conn:
            conn.execute("BEGIN")
            actor = accounts._session(conn, token)
            before_seq = None
            if before is not None:
                cursor = conn.execute("SELECT c.rowid AS seq, c.submitted_by FROM rule_candidates c "
                                      "JOIN rule_candidate_details d ON d.candidate_id=c.id WHERE c.id=?", (before,)).fetchone()
                if cursor is None or (actor["role"] != "admin" and cursor["submitted_by"] != actor["id"]):
                    raise ValueError("候选列表游标无效")
                before_seq = cursor["seq"]
            if actor["role"] == "admin":
                rows = conn.execute("SELECT c.id FROM rule_candidates c JOIN rule_candidate_details d ON d.candidate_id=c.id WHERE (? IS NULL OR c.rowid<?) ORDER BY c.rowid DESC LIMIT ?", (before_seq, before_seq, limit + 1)).fetchall()
            else:
                rows = conn.execute("SELECT c.id FROM rule_candidates c JOIN rule_candidate_details d ON d.candidate_id=c.id WHERE c.submitted_by=? AND (? IS NULL OR c.rowid<?) ORDER BY c.rowid DESC LIMIT ?", (actor["id"], before_seq, before_seq, limit + 1)).fetchall()
        items = []
        for row in rows[:limit]:
            item = ledger.get(token, row["id"], verify_environment=False)
            items.append({key: item[key] for key in ("id", "category", "sha256", "base_generation", "submitted_by", "created_at", "status")})
        has_more = len(rows) > limit
        return _response({"candidates": items, "has_more": has_more,
                          "next_before": rows[limit - 1]["id"] if has_more else None})
    except (AuthenticationError, AuthorizationError, ValueError, RuntimeError, OSError, sqlite3.Error, yaml.YAMLError) as exc:
        raise _failure(exc) from exc


@router.get("/candidates/{candidate_id}")
def candidate(candidate_id: str, request: Request):
    accounts, token = _accounts(request)
    try:
        RuleValidationService(accounts)
        details = RuleSubmissionService(accounts).get(token, candidate_id)
        state = RuleReviewLedger(accounts, versioned=True).get(token, candidate_id)
        with closing(accounts._connect()) as conn:
            conn.execute("BEGIN")
            actor = accounts._session(conn, token)
            if actor["role"] != "admin" and actor["id"] != details["submitted_by"]:
                raise AuthorizationError("无权查看该候选规则")
            submitter = conn.execute("SELECT display_name, username FROM account_users WHERE id=?", (details["submitted_by"],)).fetchone()
            review = state["review"]
            if review:
                reviewer = conn.execute("SELECT display_name, username FROM account_users WHERE id=?", (review["reviewer_id"],)).fetchone()
                review = {**review, "reviewer_display_name": reviewer["display_name"] if reviewer else "",
                          "reviewer_username": reviewer["username"] if reviewer else ""}
        return _response({**details, "submitted_by_display_name": submitter["display_name"] if submitter else "",
                          "submitted_by_username": submitter["username"] if submitter else "",
                          "status": state["status"], "review": review})
    except (AuthenticationError, AuthorizationError, ValueError, RuntimeError, OSError, sqlite3.Error, yaml.YAMLError) as exc:
        raise _failure(exc) from exc


@router.post("/candidates/{candidate_id}/validate")
def validate(candidate_id: str, request: Request):
    accounts, token = _accounts(request, write=True)
    try:
        return _response(RuleValidationService(accounts).validate(token, candidate_id))
    except (AuthenticationError, AuthorizationError, ValueError, RuntimeError, OSError, sqlite3.Error, yaml.YAMLError) as exc:
        raise _failure(exc) from exc


@router.get("/candidates/{candidate_id}/validations")
def validations(candidate_id: str, request: Request):
    accounts, token = _accounts(request)
    try:
        return _response({"validations": RuleValidationService(accounts).list_results(token, candidate_id)})
    except (AuthenticationError, AuthorizationError, ValueError, RuntimeError, OSError, sqlite3.Error, yaml.YAMLError) as exc:
        raise _failure(exc) from exc


@router.post("/candidates/{candidate_id}/review")
def review(candidate_id: str, body: ReviewInput, request: Request):
    accounts, token = _accounts(request, write=True)
    try:
        return _response(RuleReviewLedger(accounts, versioned=True).review(
            token, candidate_id, body.candidate_sha256, body.decision, body.reason,
            expected_validation_id=body.expected_validation_id,
        ))
    except (AuthenticationError, AuthorizationError, ValueError, RuntimeError, OSError, sqlite3.Error, yaml.YAMLError) as exc:
        raise _failure(exc) from exc
