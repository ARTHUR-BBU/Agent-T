"""Internal account-bound pipeline worker; no HTTP upload or production isolation."""
from __future__ import annotations

import hashlib
import json
import hmac
import os
import sqlite3
from contextlib import closing
from pathlib import Path

from app.graph.pipeline import run_review
from app.services import llm_budget, owned_llm_transport, stance
from app.services.account_store import AuthorizationError
from app.services.owned_llm_ledger import OwnedLLMLedger
from app.services.owned_review_jobs import OwnedReviewJobs
from app.services.owned_review_precheck import _RESULT_SCHEMA
from app.services.rule_execution_trial import read_trial_versions
from app.services import store as store_module
from app.services.store import store as shared_store

_CLAIM_SCHEMA = """CREATE TABLE owned_review_worker_claims (
    job_id TEXT PRIMARY KEY REFERENCES owned_review_jobs(id) ON DELETE CASCADE,
    claimed_at REAL NOT NULL
)"""
_RESULT_KEYS = frozenset({"text", "items", "policies", "error", "clause_index", "scorecard", "blind_candidates",
                          "blind_skipped_messages", "blind_skipped_reason", "blind_enabled", "quality", "objections",
                          "document_version", "completion", "facts", "verify", "rule_pack", "evidence_index"})


class OwnedReviewWorker:
    def __init__(self, jobs: OwnedReviewJobs, ledger: OwnedLLMLedger) -> None:
        if ledger.jobs is not jobs:
            raise ValueError("账户任务与调用账本不一致")
        account_path = Path(jobs.accounts.path).resolve()
        shared_path = Path(shared_store._path).resolve()
        if store_module._ACCOUNT_MODE == "1":
            configured = Path(store_module._ACCOUNT_PATH).resolve()
            if account_path != configured and not (account_path.exists() and configured.exists() and os.path.samefile(account_path, configured)):
                raise ValueError("账户合同库与启动配置不一致")
        if account_path == shared_path or (account_path.exists() and shared_path.exists() and os.path.samefile(account_path, shared_path)):
            raise ValueError("账户合同库必须独立于共享合同库")
        self.jobs, self.ledger = jobs, ledger
        with closing(jobs.accounts._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='owned_review_worker_claims'").fetchone()
            if row is None:
                conn.execute(_CLAIM_SCHEMA)
                row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='owned_review_worker_claims'").fetchone()
            if (not row or " ".join(row["sql"].split()).lower() != " ".join(_CLAIM_SCHEMA.split()).lower()
                    or conn.execute("PRAGMA foreign_key_check(owned_review_worker_claims)").fetchone() is not None):
                raise RuntimeError("账户工作器结构无效，需要离线迁移")

    def run(self, token: str, job_id: str, secret: str, *, raw: bytes, declared_stance: str = "neutral") -> str:
        if not owned_llm_transport.enabled():
            raise RuntimeError("账户模型执行模式未启用")
        if not isinstance(raw, bytes) or not raw or len(raw) > 10 * 1024 * 1024:
            raise ValueError("合同内容无效")
        rid = self.jobs.authorize_outbound(token, job_id, secret)
        original = self.jobs.owned.get(token, rid)
        if original is None:
            raise AuthorizationError("审查任务不可用")
        category, filename = original.get("category"), original.get("filename")
        if not isinstance(category, str) or not isinstance(filename, str):
            raise ValueError("审查任务内容无效")
        expected_digest = original.get("input_sha256")
        if (not isinstance(expected_digest, str) or not hmac.compare_digest(expected_digest, hashlib.sha256(raw).hexdigest())
                or original.get("input_stance") != declared_stance):
            raise ValueError("合同内容或立场与已创建任务不一致")
        versions = read_trial_versions(Path(self.jobs.accounts.path).resolve())
        selected = versions.get(category)
        if selected is None:
            raise ValueError("账户规则基线不存在")
        configuration = selected.snapshot.configuration()
        if configuration.get("hidden") or not stance.is_allowed(category, declared_stance, configuration=configuration):
            raise ValueError("合同立场无效")
        provenance = {"source": "database_test", "category": category, "content_sha256": selected.sha256,
                      "version_id": selected.version_id, "generation": selected.generation}
        review_budget = None
        prechecked = False
        with closing(self.jobs.accounts._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            self.jobs._authorize_outbound_in_transaction(conn, token, job_id, secret)
            has_precheck = bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='owned_review_precheck_claims'").fetchone()
                                and conn.execute("SELECT 1 FROM owned_review_precheck_claims WHERE job_id=?", (job_id,)).fetchone())
            prepared = None
            if has_precheck:
                schema = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='owned_review_precheck_results'").fetchone()
                if not schema or " ".join(schema["sql"].split()).lower() != " ".join(_RESULT_SCHEMA.split()).lower():
                    raise RuntimeError("账户预审结果尚不可用")
                prepared = conn.execute("SELECT * FROM owned_review_precheck_results WHERE job_id=?", (job_id,)).fetchone()
                if prepared is None or prepared["state"] != "ready":
                    raise RuntimeError("账户预审确认尚未完成")
                if (prepared["selected_category"] != category or prepared["selected_stance"] != declared_stance
                        or prepared["input_sha256"] != expected_digest or prepared["input_kind"] != original.get("input_kind")):
                    raise AuthorizationError("账户预审绑定内容已变化")
                active_versions = conn.execute("SELECT a.category,a.version_id,a.generation,v.sha256 FROM rule_active_versions a "
                                               "JOIN rule_versions v ON v.id=a.version_id AND v.category=a.category ORDER BY a.category").fetchall()
                actual = [[r["category"], r["version_id"], r["generation"], r["sha256"]] for r in active_versions]
                if actual != json.loads(prepared["versions_json"]):
                    raise AuthorizationError("预审后规则版本已变化，请重新提交")
                limit, remaining = prepared["budget_limit"], prepared["budget_remaining"]
                if limit is not None:
                    if type(limit) is not int or type(remaining) is not int or limit <= 0 or not 0 <= remaining <= limit:
                        raise ValueError("账户审查预算无效")
                    review_budget = llm_budget.ReviewBudget(limit)
                    if not review_budget.try_consume(limit - remaining):
                        raise ValueError("账户审查预算无效")
                elif remaining is not None:
                    raise ValueError("账户审查预算无效")
                prechecked = True
            elif original.get("require_precheck"):
                raise AuthorizationError("账户预审尚未完成")
            active = conn.execute("SELECT a.generation,v.id,v.content,v.sha256 FROM rule_active_versions a "
                                  "JOIN rule_versions v ON v.id=a.version_id AND v.category=a.category "
                                  "WHERE a.category=?", (category,)).fetchone()
            if (active is None or active["id"] != selected.version_id or active["generation"] != selected.generation
                    or active["sha256"] != selected.sha256 or hashlib.sha256(active["content"]).hexdigest() != selected.sha256):
                raise ValueError("账户规则基线已变化")
            import time
            try:
                conn.execute("INSERT INTO owned_review_worker_claims VALUES (?,?)", (job_id, time.time()))
            except sqlite3.IntegrityError as exc:
                raise AuthorizationError("审查任务已经认领") from exc
            row = conn.execute("SELECT data FROM reviews WHERE id=?", (rid,)).fetchone()
            data = json.loads(row["data"])
            if (data.get("category") != category or data.get("filename") != filename
                    or data.get("input_sha256") != expected_digest or data.get("input_stance") != declared_stance):
                raise ValueError("审查任务内容已变化")
            data.update(rule_provenance=provenance, stance=declared_stance,
                        category_label=configuration.get("label", category),
                        stance_label_at_review=stance.stance_label(category, declared_stance, configuration=configuration))
            if prepared is not None:
                data["precheck"] = {"performed": bool(prepared["precheck_performed"]),
                                    "skip_reason": prepared["precheck_skip_reason"],
                                    "suggestion_performed": bool(prepared["suggestion_performed"]), "summary": prepared["summary"],
                                    "suspect": bool(prepared["suspect"]) or bool(prepared["required_confirmation"]),
                                    "original_category": prepared["original_category"], "original_stance": prepared["original_stance"],
                                    "selected_category": category, "selected_stance": declared_stance}
                changed = conn.execute("UPDATE owned_review_precheck_results SET state='consumed' WHERE job_id=? AND state='ready'", (job_id,)).rowcount
                if changed != 1:
                    raise AuthorizationError("账户预审确认已被使用")
            conn.execute("UPDATE reviews SET data=? WHERE id=?", (json.dumps(data, ensure_ascii=False), rid))
            self.jobs._authorize_outbound_in_transaction(conn, token, job_id, secret)
        try:
            with owned_llm_transport.job_context(self.ledger, token, job_id, secret):
                result = run_review(filename, raw, category=category, stance=declared_stance,
                                    review_id=rid, rule_snapshot=selected.snapshot,
                                    budget=review_budget if prechecked else llm_budget.new_review_budget())
            if not isinstance(result, dict) or set(result) - (_RESULT_KEYS | {"category", "category_label"}):
                raise ValueError("审查结果结构无效")
            if result.get("category") != category:
                raise ValueError("审查结果品类不一致")
            fields = {key: result[key] for key in _RESULT_KEYS if key in result}
            fields["text_preview"] = (fields.get("text") or "")[:500]
            return self.jobs.commit(token, job_id, secret, result=fields, failed=bool(result.get("error")))
        except Exception:
            try:
                self.jobs.commit(token, job_id, secret, result={"error": "审查任务未完成，请重新提交"}, failed=True)
            except Exception:
                pass  # Revoked/expired tasks remain for the internal reaper.
            raise
