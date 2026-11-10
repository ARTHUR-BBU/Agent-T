"""Account-bound intake precheck and single-use internal confirmation."""
from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import threading
import time
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.services import category_suggest, llm_budget, llm_call_log, owned_llm_transport, precheck, stance
from app.services.account_store import AuthorizationError
from app.services.extract import extract_text
from app.services.owned_llm_ledger import OwnedLLMLedger
from app.services.owned_review_jobs import OwnedReviewJobs
from app.services.rule_execution_trial import read_trial_versions

_SLOTS = threading.BoundedSemaphore(2)
_CLAIM_SCHEMA = """CREATE TABLE owned_review_precheck_claims (
    job_id TEXT PRIMARY KEY REFERENCES owned_review_jobs(id) ON DELETE CASCADE,
    created_at REAL NOT NULL
)"""
_RESULT_SCHEMA = """CREATE TABLE owned_review_precheck_results (
    job_id TEXT PRIMARY KEY REFERENCES owned_review_precheck_claims(job_id) ON DELETE CASCADE,
    state TEXT NOT NULL CHECK(state IN ('awaiting','ready','consumed')),
    original_category TEXT NOT NULL, original_stance TEXT NOT NULL,
    choices_json TEXT NOT NULL, versions_json TEXT NOT NULL,
    input_sha256 TEXT NOT NULL, input_kind TEXT NOT NULL,
    budget_limit INTEGER, budget_remaining INTEGER,
    summary TEXT NOT NULL, suspect INTEGER NOT NULL CHECK(suspect IN (0,1)),
    performed INTEGER NOT NULL CHECK(performed IN (0,1)),
    precheck_performed INTEGER NOT NULL CHECK(precheck_performed IN (0,1)), precheck_skip_reason TEXT,
    suggestion_performed INTEGER NOT NULL CHECK(suggestion_performed IN (0,1)),
    required_confirmation INTEGER NOT NULL CHECK(required_confirmation IN (0,1)),
    selected_category TEXT, selected_stance TEXT, created_at REAL NOT NULL
)"""


@dataclass(frozen=True, repr=False)
class PreparedAccountIntake:
    review_id: str
    category: str
    action: str
    suggested_category: str | None
    summary: str
    suspect: bool
    precheck_performed: bool
    precheck_skip_reason: str | None
    suggestion_performed: bool
    versions: tuple[tuple[str, str, int, str], ...]
    budget: Any = field(repr=False)


def prepare_account_intake(jobs: OwnedReviewJobs, ledger: OwnedLLMLedger, token: str, job_id: str,
                           secret: str, *, raw: bytes, declared_stance: str = "neutral") -> PreparedAccountIntake:
    """Run the existing prechecks with job authorization and reject rules drift.

    Confirmations are internal and require the original job secret. No HTTP
    route or browser ticket is provided by this service.
    """
    if not owned_llm_transport.enabled() or ledger.jobs is not jobs:
        raise RuntimeError("账户预审配置无效")
    if not isinstance(raw, bytes) or not raw or len(raw) > 10 * 1024 * 1024:
        raise ValueError("合同内容无效")
    rid = jobs.authorize_outbound(token, job_id, secret)
    row = jobs.owned.get(token, rid)
    if row is None:
        raise AuthorizationError("审查任务不可用")
    expected = row.get("input_sha256")
    if (not isinstance(expected, str) or not hmac.compare_digest(expected, hashlib.sha256(raw).hexdigest())
            or row.get("input_stance") != declared_stance):
        raise ValueError("合同内容或立场与已创建任务不一致")
    filename, category = row.get("filename"), row.get("category")
    if not isinstance(filename, str) or not isinstance(category, str):
        raise ValueError("审查任务内容无效")
    versions = read_trial_versions(Path(jobs.accounts.path).resolve())
    selected = versions.get(category)
    if selected is None:
        raise ValueError("账户规则基线不存在")
    config = selected.snapshot.configuration()
    if config.get("hidden") or not stance.is_allowed(category, declared_stance, configuration=config):
        raise ValueError("合同立场无效")
    captured = tuple((key, version.version_id, version.generation, version.sha256) for key, version in versions.items())
    categories = [{"id": key, "label": cfg.get("label", key), "stances": cfg.get("stances") or {}}
                  for key, version in versions.items() if not (cfg := version.snapshot.configuration()).get("hidden")]
    if not _SLOTS.acquire(blocking=False):
        raise RuntimeError("账户预审繁忙，请稍后重试")
    try:
        budget = llm_budget.new_review_budget()
        with closing(jobs.accounts._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            jobs._authorize_outbound_in_transaction(conn, token, job_id, secret)
            current = conn.execute("SELECT data FROM reviews WHERE id=?", (rid,)).fetchone()
            current_data = json.loads(current["data"]) if current else {}
            if (current_data.get("input_sha256") != expected or current_data.get("input_stance") != declared_stance
                    or current_data.get("category") != category or current_data.get("filename") != filename):
                raise ValueError("预审前任务内容已变化，请重新提交")
            active = conn.execute("SELECT a.category,a.version_id,a.generation,v.sha256 FROM rule_active_versions a "
                                  "JOIN rule_versions v ON v.id=a.version_id AND v.category=a.category ORDER BY a.category").fetchall()
            if tuple((item["category"], item["version_id"], item["generation"], item["sha256"]) for item in active) != captured:
                raise ValueError("预审前规则版本已变化，请重新提交")
            if (conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='owned_review_worker_claims'").fetchone()
                    and conn.execute("SELECT 1 FROM owned_review_worker_claims WHERE job_id=?", (job_id,)).fetchone()):
                raise AuthorizationError("审查任务已经启动，无法预审")
            schema = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='owned_review_precheck_claims'").fetchone()
            if schema is None:
                conn.execute(_CLAIM_SCHEMA)
                schema = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='owned_review_precheck_claims'").fetchone()
            if (not schema or " ".join(schema["sql"].split()).lower() != " ".join(_CLAIM_SCHEMA.split()).lower()
                    or conn.execute("PRAGMA foreign_key_check(owned_review_precheck_claims)").fetchone() is not None):
                raise RuntimeError("账户预审结构无效，需要离线迁移")
            try:
                conn.execute("INSERT INTO owned_review_precheck_claims VALUES (?,?)", (job_id, time.time()))
            except sqlite3.IntegrityError as exc:
                raise AuthorizationError("该任务已经预审，请重新提交") from exc
            jobs._authorize_outbound_in_transaction(conn, token, job_id, secret)
        text = extract_text(filename, raw)
        with closing(jobs.accounts._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            jobs._authorize_outbound_in_transaction(conn, token, job_id, secret)
            active = conn.execute("SELECT a.category,a.version_id,a.generation,v.sha256 FROM rule_active_versions a "
                                  "JOIN rule_versions v ON v.id=a.version_id AND v.category=a.category ORDER BY a.category").fetchall()
            if tuple((item["category"], item["version_id"], item["generation"], item["sha256"]) for item in active) != captured:
                raise ValueError("预审期间规则版本已变化，请重新提交")
        with owned_llm_transport.job_context(ledger, token, job_id, secret):
            with llm_call_log.record_node("category_suggest", rid, prompt_version=category_suggest.PROMPT_VERSION):
                suggestion = category_suggest.suggest_category(text, category, llm_budget.new_suggest_budget())
            performed = bool(suggestion.performed)
            precheck_performed = False
            precheck_skip_reason = "category_confirmation" if suggestion.performed else None
            if suggestion.performed and suggestion.result and suggestion.result.category not in {None, category, "other"}:
                candidate = suggestion.result.category
                available = {item["id"] for item in categories}
                action, suggested = "confirm", candidate if candidate in available else None
                summary, suspect = category_suggest.category_zh(candidate), False
            else:
                with llm_call_log.record_node("precheck", rid):
                    check = precheck.run_precheck(text, category, budget=budget, stance=declared_stance,
                                                  categories=categories,
                                                  stance_label=stance.stance_label(category, declared_stance, configuration=config))
                branch = precheck.decide_branch(check, category)
                performed = performed or check.performed
                precheck_performed = bool(check.performed)
                precheck_skip_reason = check.skip_reason
                action = "proceed" if branch["action"] == "proceed" else "confirm"
                suggested = check.result.suggested_category if check.result and check.result.is_supported else None
                summary = check.result.summary if check.result else ""
                suspect = bool(branch["suspect"])
                if suggestion.performed and suggestion.result and suggestion.result.category == category:
                    suspect = suspect or category_suggest.confidence_tier(suggestion.result.confidence or 0.0) == "low"
        with closing(jobs.accounts._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            jobs._authorize_outbound_in_transaction(conn, token, job_id, secret)
            current_review = conn.execute("SELECT data FROM reviews WHERE id=?", (rid,)).fetchone()
            current_data = json.loads(current_review["data"]) if current_review else {}
            if (current_data.get("input_sha256") != expected or current_data.get("input_stance") != declared_stance
                    or current_data.get("category") != category or current_data.get("filename") != filename):
                raise ValueError("预审期间任务内容已变化，请重新提交")
            active = conn.execute("SELECT a.category,a.version_id,a.generation,v.sha256 FROM rule_active_versions a "
                                  "JOIN rule_versions v ON v.id=a.version_id AND v.category=a.category ORDER BY a.category").fetchall()
            current = tuple((item["category"], item["version_id"], item["generation"], item["sha256"]) for item in active)
            if current != captured:
                raise ValueError("预审期间规则版本已变化，请重新提交")
            schema = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='owned_review_precheck_results'").fetchone()
            if schema is None:
                conn.execute(_RESULT_SCHEMA)
                schema = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='owned_review_precheck_results'").fetchone()
            if (not schema or " ".join(schema["sql"].split()).lower() != " ".join(_RESULT_SCHEMA.split()).lower()
                    or conn.execute("PRAGMA foreign_key_check(owned_review_precheck_results)").fetchone() is not None):
                raise RuntimeError("账户预审结果结构无效，需要离线迁移")
            choices = {category: declared_stance}
            if action == "confirm" and suggested and suggested != category and suggested in versions:
                suggested_config = versions[suggested].snapshot.configuration()
                if not suggested_config.get("hidden") and stance.is_allowed(suggested, "neutral", configuration=suggested_config):
                    choices[suggested] = "neutral"
                else:
                    suggested = None
            limit = budget.limit() if budget is not None else None
            remaining = budget.remaining() if budget is not None else None
            input_kind = current_data.get("input_kind")
            if input_kind not in ("file", "pasted_text"):
                raise ValueError("合同输入方式无效")
            conn.execute("INSERT INTO owned_review_precheck_results VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                job_id, "awaiting" if action == "confirm" else "ready", category, declared_stance,
                json.dumps(choices, ensure_ascii=False, sort_keys=True), json.dumps(captured, ensure_ascii=False),
                expected, input_kind, limit, remaining, summary, int(suspect), int(performed), int(precheck_performed),
                precheck_skip_reason, int(suggestion.performed), int(action == "confirm"),
                None if action == "confirm" else category, None if action == "confirm" else declared_stance, time.time(),
            ))
            jobs._authorize_outbound_in_transaction(conn, token, job_id, secret)
        return PreparedAccountIntake(rid, category, action, suggested, summary, suspect, precheck_performed,
                                     precheck_skip_reason, bool(suggestion.performed), captured, budget)
    finally:
        _SLOTS.release()


def confirm_account_intake(jobs: OwnedReviewJobs, token: str, job_id: str, secret: str, *,
                           raw: bytes, input_kind: str, category: str) -> str:
    """Record one human choice, with the job secret held only by trusted server code."""
    if not isinstance(raw, bytes) or not raw or len(raw) > 10 * 1024 * 1024 or input_kind not in ("file", "pasted_text"):
        raise ValueError("合同输入无效")
    with closing(jobs.accounts._connect()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        rid = jobs._authorize_outbound_in_transaction(conn, token, job_id, secret)
        schema = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='owned_review_precheck_results'").fetchone()
        if not schema or " ".join(schema["sql"].split()).lower() != " ".join(_RESULT_SCHEMA.split()).lower():
            raise RuntimeError("账户预审结果结构无效")
        prepared = conn.execute("SELECT * FROM owned_review_precheck_results WHERE job_id=?", (job_id,)).fetchone()
        review = conn.execute("SELECT data FROM reviews WHERE id=?", (rid,)).fetchone()
        if prepared is None or prepared["state"] != "awaiting" or review is None:
            raise AuthorizationError("确认已失效，请重新提交")
        data = json.loads(review["data"])
        if (not hmac.compare_digest(prepared["input_sha256"], hashlib.sha256(raw).hexdigest())
                or data.get("input_sha256") != prepared["input_sha256"] or data.get("input_kind") != input_kind
                or input_kind != prepared["input_kind"] or data.get("category") != prepared["original_category"]
                or data.get("input_stance") != prepared["original_stance"]):
            raise AuthorizationError("合同或预审内容已变化，请重新提交")
        active = conn.execute("SELECT a.category,a.version_id,a.generation,v.sha256 FROM rule_active_versions a "
                              "JOIN rule_versions v ON v.id=a.version_id AND v.category=a.category ORDER BY a.category").fetchall()
        current = [[row["category"], row["version_id"], row["generation"], row["sha256"]] for row in active]
        choices = json.loads(prepared["choices_json"])
        if current != json.loads(prepared["versions_json"]) or not isinstance(choices, dict) or category not in choices:
            raise AuthorizationError("规则版本或确认选项已变化，请重新提交")
        selected_stance = choices[category]
        if not isinstance(selected_stance, str):
            raise AuthorizationError("确认选项无效")
        data.update(category=category, input_stance=selected_stance)
        conn.execute("UPDATE reviews SET data=? WHERE id=?", (json.dumps(data, ensure_ascii=False), rid))
        changed = conn.execute("UPDATE owned_review_precheck_results SET state='ready',selected_category=?,selected_stance=? "
                               "WHERE job_id=? AND state='awaiting'", (category, selected_stance, job_id)).rowcount
        if changed != 1:
            raise AuthorizationError("确认已失效，请重新提交")
        jobs._authorize_outbound_in_transaction(conn, token, job_id, secret)
        return selected_stance
