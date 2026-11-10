"""Authenticated, version-bound submission of constrained rule proposals.

The record stores supplied examples in plaintext and is not yet exposed to the
application UI. Callers must establish a suitable data-retention policy first.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
import uuid
from contextlib import closing
from typing import Any, Mapping

from app.services.account_store import AccountStore, AuthorizationError
from app.services.rule_candidate_builder import build_candidate
from app.services.rule_review_ledger import RuleReviewLedger, _validate_candidate_changes
from app.services.rule_snapshot import RuleSnapshot


class RuleSubmissionService:
    def __init__(self, accounts: AccountStore) -> None:
        self.accounts = accounts
        self.ledger = RuleReviewLedger(accounts, versioned=True)
        with closing(accounts._connect()) as conn, conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS rule_candidate_details (
                    candidate_id TEXT PRIMARY KEY REFERENCES rule_candidates(id),
                    reason TEXT NOT NULL, change_json TEXT NOT NULL, examples_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS rule_submission_requests (
                    actor_id TEXT NOT NULL REFERENCES account_users(id), request_id TEXT NOT NULL,
                    fingerprint TEXT NOT NULL, candidate_id TEXT NOT NULL REFERENCES rule_candidates(id),
                    PRIMARY KEY(actor_id, request_id)
                );
                CREATE TRIGGER IF NOT EXISTS candidate_details_no_update BEFORE UPDATE ON rule_candidate_details
                BEGIN SELECT RAISE(ABORT, 'candidate details are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS candidate_details_no_delete BEFORE DELETE ON rule_candidate_details
                BEGIN SELECT RAISE(ABORT, 'candidate details are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS candidate_details_no_replace BEFORE INSERT ON rule_candidate_details
                WHEN EXISTS (SELECT 1 FROM rule_candidate_details WHERE candidate_id=NEW.candidate_id)
                BEGIN SELECT RAISE(ABORT, 'candidate details are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS submission_request_no_update BEFORE UPDATE ON rule_submission_requests
                BEGIN SELECT RAISE(ABORT, 'submission requests are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS submission_request_no_delete BEFORE DELETE ON rule_submission_requests
                BEGIN SELECT RAISE(ABORT, 'submission requests are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS submission_request_no_replace BEFORE INSERT ON rule_submission_requests
                WHEN EXISTS (SELECT 1 FROM rule_submission_requests WHERE actor_id=NEW.actor_id AND request_id=NEW.request_id)
                BEGIN SELECT RAISE(ABORT, 'submission requests are append-only'); END;
            """)

    @staticmethod
    def _existing(conn: sqlite3.Connection, actor_id: str, request_id: str, fingerprint: str) -> dict[str, Any] | None:
        row = conn.execute("SELECT r.fingerprint, c.* FROM rule_submission_requests r "
                           "JOIN rule_candidates c ON c.id=r.candidate_id "
                           "WHERE r.actor_id=? AND r.request_id=?", (actor_id, request_id)).fetchone()
        if row is None:
            return None
        if row["fingerprint"] != fingerprint:
            raise ValueError("请求编号已用于不同的变更")
        if (hashlib.sha256(row["content"]).hexdigest() != row["sha256"] or
                hashlib.sha256(row["baseline"]).hexdigest() != row["base_sha256"]):
            raise ValueError("候选规则内容损坏")
        return {key: row[key] for key in ("id", "category", "sha256", "base_sha256", "base_generation", "submitted_by", "created_at")}

    def submit(self, token: str, draft: Mapping[str, Any], *, expected_generation: int,
               request_id: str) -> dict[str, Any]:
        if not isinstance(expected_generation, int) or isinstance(expected_generation, bool) or expected_generation < 1:
            raise ValueError("规则基线代次无效")
        if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", request_id):
            raise ValueError("请求编号无效")
        fields = dict(draft)
        allowed = ("category", "operation", "item_id", "name", "description", "reason", "segment",
                   "literal", "positive", "negative", "base_sha256")
        payload = {key: fields.get(key) for key in allowed}
        if any(value is not None and (not isinstance(value, str) or len(value) > 2000) for value in payload.values()):
            raise ValueError("变更输入无效")
        fingerprint = hashlib.sha256(json.dumps({"generation": expected_generation, "draft": payload},
                                                 sort_keys=True, ensure_ascii=True).encode("utf-8")).hexdigest()
        category = fields.get("category")
        if not isinstance(category, str):
            raise ValueError("合同类型无效")
        with closing(self.accounts._connect()) as conn:
            conn.execute("BEGIN")
            actor = self.accounts._session(conn, token)
            previous = self._existing(conn, actor["id"], request_id, fingerprint)
            if previous is not None:
                return previous
            baseline, generation = self.ledger._baseline(conn, category)
            if generation != expected_generation:
                raise ValueError("规则基线代次已变化")
            actor_id = actor["id"]
        base_sha256 = hashlib.sha256(baseline).hexdigest()
        snapshot = RuleSnapshot(category, baseline, base_sha256[:12])
        built = build_candidate(snapshot, fields)
        _validate_candidate_changes(snapshot.configuration(), built.snapshot.configuration())
        digest = hashlib.sha256(built.snapshot.config_bytes).hexdigest()
        with closing(self.accounts._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            actor = self.accounts._session(conn, token)
            previous = self._existing(conn, actor["id"], request_id, fingerprint)
            if previous is not None:
                return previous
            current, current_generation = self.ledger._baseline(conn, category)
            if actor["id"] != actor_id or current_generation != generation or hashlib.sha256(current).hexdigest() != base_sha256:
                raise ValueError("规则基线代次已变化")
            candidate_id, now = uuid.uuid4().hex, time.time()
            conn.execute("INSERT INTO rule_candidates VALUES (?,?,?,?,?,?,?,?,?)", (
                candidate_id, category, built.snapshot.config_bytes, digest, base_sha256, baseline,
                generation, actor["id"], now,
            ))
            examples: dict[str, Any] = {"results": built.example_results}
            if fields.get("operation") == "add":
                examples.update(positive=fields["positive"], negative=fields["negative"])
            conn.execute("INSERT INTO rule_candidate_details VALUES (?,?,?,?)", (
                candidate_id, built.reason, json.dumps(built.change, ensure_ascii=False, sort_keys=True),
                json.dumps(examples, ensure_ascii=False, sort_keys=True),
            ))
            conn.execute("INSERT INTO rule_submission_requests VALUES (?,?,?,?)", (
                actor["id"], request_id, fingerprint, candidate_id,
            ))
            self.accounts._audit(conn, actor["id"], "submit_rule_candidate", candidate_id)
            return {"id": candidate_id, "category": category, "sha256": digest,
                    "base_sha256": base_sha256, "base_generation": generation,
                    "submitted_by": actor["id"], "created_at": now}

    def get(self, token: str, candidate_id: str) -> dict[str, Any]:
        with closing(self.accounts._connect()) as conn:
            conn.execute("BEGIN")
            actor = self.accounts._session(conn, token)
            row = conn.execute("SELECT c.*, d.reason, d.change_json, d.examples_json "
                               "FROM rule_candidates c JOIN rule_candidate_details d ON d.candidate_id=c.id "
                               "WHERE c.id=?", (candidate_id,)).fetchone()
            if row is None:
                raise ValueError("候选规则不存在")
            if actor["role"] != "admin" and row["submitted_by"] != actor["id"]:
                raise AuthorizationError("无权查看该候选规则")
            if (hashlib.sha256(row["content"]).hexdigest() != row["sha256"] or
                    hashlib.sha256(row["baseline"]).hexdigest() != row["base_sha256"]):
                raise ValueError("候选规则内容损坏")
            return {"id": row["id"], "category": row["category"], "sha256": row["sha256"],
                    "base_sha256": row["base_sha256"], "base_generation": row["base_generation"],
                    "submitted_by": row["submitted_by"], "created_at": row["created_at"],
                    "reason": row["reason"], "change": json.loads(row["change_json"]),
                    "examples": json.loads(row["examples_json"])}
