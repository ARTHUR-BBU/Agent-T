"""Local candidate and human-review ledger; never publishes effective rules.

This is not a release gate: no automated regression evidence is recorded here.
Callers must not interpret an approval as permission to publish.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
import uuid
from contextlib import closing
from typing import Any

from app.services.account_store import AccountStore, AuthorizationError
from app.services.rule_candidate_validation import current_input_manifest
from app.services.rule_candidate_builder import validate_candidate_conflicts
from app.services.rule_snapshot import RuleSnapshot, capture_rule_snapshot
from app.services.rule_validation_guard import require_validation_ready


def _contains_hardline(value: Any) -> bool:
    if isinstance(value, dict):
        return str(value.get("class") or "").strip().lower() == "hardline" or any(
            _contains_hardline(v) for v in value.values()
        )
    return isinstance(value, list) and any(_contains_hardline(v) for v in value)


def _validate_candidate_changes(baseline: dict[str, Any], candidate: dict[str, Any]) -> None:
    """Allow the existing UI's constrained changes; complex rule editing needs another gate."""
    if {k: v for k, v in baseline.items() if k != "items"} != {
        k: v for k, v in candidate.items() if k != "items"
    }:
        raise ValueError("普通规则变更不能修改品类和评分配置")
    before, after = baseline["items"], candidate["items"]
    if [item["id"] for item in after[:len(before)]] != [item["id"] for item in before]:
        raise ValueError("普通规则变更不能删除或重排已有检查项")
    if len(after) > len(before) + 1 or sum(old != new for old, new in zip(before, after)) + max(0, len(after) - len(before)) > 1:
        raise ValueError("一次候选只能修改一个检查项")
    for old, new in zip(before, after):
        if _contains_hardline(old) and old != new:
            raise ValueError("普通规则变更不能修改硬性规则")
        if {k: v for k, v in old.items() if k not in {"name", "description", "enabled"}} != {
            k: v for k, v in new.items() if k not in {"name", "description", "enabled"}
        }:
            raise ValueError("已有检查项仅支持修改名称、说明和启停状态")
        if any(old.get(key) != new.get(key) for key in ("name", "description")):
            _validate_display_fields(new)
    for item in after[len(before):]:
        if set(item) != {"id", "name", "description", "class", "segment", "rules"} or item["class"] != "existence":
            raise ValueError("新增检查项仅支持字面关键词")
        _validate_display_fields(item)
        if not isinstance(item["id"], str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", item["id"]):
            raise ValueError("新增检查项编号无效")
        if not isinstance(item["segment"], str) or not item["segment"].strip():
            raise ValueError("新增检查项评分分段无效")
        rules = item["rules"]
        if not isinstance(rules, dict) or set(rules) != {"pass"} or not isinstance(rules["pass"], list) or len(rules["pass"]) != 1:
            raise ValueError("新增检查项仅支持字面关键词")
        rule = rules["pass"][0]
        if not isinstance(rule, dict) or set(rule) != {"pattern"} or not isinstance(rule["pattern"], str):
            raise ValueError("新增检查项仅支持字面关键词")
        pattern = rule["pattern"]
        literal = re.sub(r"\\(.)", r"\1", pattern, flags=re.DOTALL)
        if not literal or len(literal) > 120 or re.escape(literal) != pattern:
            raise ValueError("新增检查项仅支持字面关键词，不能使用正则")
    validate_candidate_conflicts(baseline, candidate)


def _validate_display_fields(item: dict[str, Any]) -> None:
    for key, maximum in (("name", 120), ("description", 1000)):
        value = item.get(key)
        if not isinstance(value, str) or not value.strip() or len(value) > maximum:
            raise ValueError("检查项名称和说明须为有效文本")


class RuleReviewLedger:
    """Append-only candidate bytes and decisions using the account DB transaction."""

    def __init__(self, accounts: AccountStore, *, versioned: bool = False) -> None:
        self.accounts = accounts
        self.versioned = versioned
        if versioned:
            from app.services.rule_versions import RuleVersionStore
            RuleVersionStore(accounts)
        with closing(accounts._connect()) as conn, conn:
            existing = conn.execute("PRAGMA table_info(rule_candidates)").fetchall()
            if existing and not {"baseline", "base_generation"}.issubset({row["name"] for row in existing}):
                raise RuntimeError("旧版规则候选数据库需要安全迁移；请使用新的试验库")
            reviews = conn.execute("PRAGMA table_info(rule_candidate_reviews)").fetchall()
            if reviews and "validation_id" not in {row["name"] for row in reviews}:
                raise RuntimeError("旧版规则审核数据库需要安全迁移；请使用新的试验库")
            for index in conn.execute("PRAGMA index_list(rule_candidate_reviews)"):
                columns = [row["name"] for row in conn.execute("SELECT name FROM pragma_index_info(?)", (index["name"],))]
                if index["unique"] and columns == ["candidate_id"]:
                    raise RuntimeError("旧版规则审核数据库需要安全迁移；请使用新的试验库")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS rule_candidates (
                    id TEXT PRIMARY KEY, category TEXT NOT NULL, content BLOB NOT NULL,
                    sha256 TEXT NOT NULL, base_sha256 TEXT NOT NULL, baseline BLOB NOT NULL,
                    base_generation INTEGER,
                    submitted_by TEXT NOT NULL REFERENCES account_users(id), created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS rule_candidate_reviews (
                    id TEXT PRIMARY KEY, candidate_id TEXT NOT NULL REFERENCES rule_candidates(id),
                    candidate_sha256 TEXT NOT NULL, reviewer_id TEXT NOT NULL REFERENCES account_users(id),
                    decision TEXT NOT NULL CHECK(decision IN ('approve','reject')),
                    reason TEXT NOT NULL, self_review INTEGER NOT NULL CHECK(self_review IN (0,1)),
                    validation_id TEXT,
                    created_at REAL NOT NULL
                );
                CREATE TRIGGER IF NOT EXISTS candidate_no_update BEFORE UPDATE ON rule_candidates
                BEGIN SELECT RAISE(ABORT, 'candidates are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS candidate_no_replace BEFORE INSERT ON rule_candidates
                WHEN EXISTS (SELECT 1 FROM rule_candidates WHERE id=NEW.id)
                BEGIN SELECT RAISE(ABORT, 'candidates are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS candidate_no_delete BEFORE DELETE ON rule_candidates
                BEGIN SELECT RAISE(ABORT, 'candidates are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS candidate_review_no_update BEFORE UPDATE ON rule_candidate_reviews
                BEGIN SELECT RAISE(ABORT, 'reviews are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS candidate_review_no_replace BEFORE INSERT ON rule_candidate_reviews
                WHEN EXISTS (SELECT 1 FROM rule_candidate_reviews WHERE id=NEW.id)
                BEGIN SELECT RAISE(ABORT, 'reviews are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS candidate_review_no_delete BEFORE DELETE ON rule_candidate_reviews
                BEGIN SELECT RAISE(ABORT, 'reviews are append-only'); END;
            """)

    def _baseline(self, conn: sqlite3.Connection, category: str) -> tuple[bytes, int | None]:
        if not self.versioned:
            return capture_rule_snapshot(category).config_bytes, None
        row = conn.execute(
            "SELECT v.content, v.sha256, a.generation FROM rule_active_versions a "
            "JOIN rule_versions v ON v.id=a.version_id "
            "WHERE a.category=? AND v.category=a.category", (category,),
        ).fetchone()
        if row is None:
            raise ValueError("品类规则版本未初始化")
        raw = row["content"]
        if hashlib.sha256(raw).hexdigest() != row["sha256"]:
            raise ValueError("规则版本内容损坏")
        return raw, row["generation"]

    def submit(self, token: str, category: str, content: bytes, base_sha256: str) -> dict[str, Any]:
        self.accounts.authenticate(token)
        if not isinstance(content, bytes) or not content or len(content) > 2_000_000:
            raise ValueError("规则候选内容无效或过大")
        if not isinstance(base_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", base_sha256):
            raise ValueError("基线摘要无效")
        digest = hashlib.sha256(content).hexdigest()
        candidate = RuleSnapshot(category=category, config_bytes=content, content_version=digest[:12])
        with closing(self.accounts._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            actor = self.accounts._session(conn, token)
            baseline, generation = self._baseline(conn, category)
            actual_baseline = hashlib.sha256(baseline).hexdigest()
            if actual_baseline != base_sha256:
                raise ValueError("规则基线已变化")
            base_snapshot = RuleSnapshot(category=category, config_bytes=baseline, content_version=actual_baseline[:12])
            _validate_candidate_changes(base_snapshot.configuration(), candidate.configuration())
            candidate_id, now = uuid.uuid4().hex, time.time()
            conn.execute("INSERT INTO rule_candidates VALUES (?,?,?,?,?,?,?,?,?)", (
                candidate_id, category, content, digest, base_sha256, baseline, generation, actor["id"], now,
            ))
            return {"id": candidate_id, "category": category, "sha256": digest,
                    "base_sha256": base_sha256, "base_generation": generation,
                    "submitted_by": actor["id"], "created_at": now}

    def review(self, token: str, candidate_id: str, expected_sha256: str,
               decision: str, reason: str, *, expected_validation_id: str | None = None) -> dict[str, Any]:
        if decision not in {"approve", "reject"} or not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 1000:
            raise ValueError("请选择审核决定并填写理由")
        current_manifest = None
        if self.versioned and decision == "approve":
            with closing(self.accounts._connect()) as conn:
                conn.execute("BEGIN")
                self.accounts._session(conn, token, admin=True)
                source = conn.execute("SELECT category, content, sha256 FROM rule_candidates WHERE id=?", (candidate_id,)).fetchone()
                if source is None:
                    raise ValueError("候选规则不存在")
                digest = hashlib.sha256(source["content"]).hexdigest()
                if digest != source["sha256"] or digest != expected_sha256:
                    raise ValueError("候选规则内容已变化")
                snapshot = RuleSnapshot(source["category"], source["content"], digest[:12])
            current_manifest = current_input_manifest(snapshot)
        with closing(self.accounts._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            actor = self.accounts._session(conn, token, admin=True)
            if self.versioned and decision == "approve":
                require_validation_ready(conn)
            candidate = conn.execute("SELECT * FROM rule_candidates WHERE id=?", (candidate_id,)).fetchone()
            if candidate is None:
                raise ValueError("候选规则不存在")
            actual = hashlib.sha256(candidate["content"]).hexdigest()
            if actual != candidate["sha256"] or actual != expected_sha256:
                raise ValueError("候选规则内容已变化")
            baseline, generation = self._baseline(conn, candidate["category"])
            if (hashlib.sha256(baseline).hexdigest() != candidate["base_sha256"]
                    or generation != candidate["base_generation"]):
                raise ValueError("规则基线已变化")
            validation_id = None
            if self.versioned and decision == "approve":
                try:
                    detail = conn.execute("SELECT 1 FROM rule_candidate_details WHERE candidate_id=?", (candidate_id,)).fetchone()
                    validation = conn.execute(
                        "SELECT * FROM rule_validation_results WHERE candidate_id=? ORDER BY rowid DESC LIMIT 1",
                        (candidate_id,),
                    ).fetchone()
                    job = conn.execute(
                        "SELECT * FROM rule_validation_jobs WHERE candidate_id=? ORDER BY rowid DESC LIMIT 1",
                        (candidate_id,),
                    ).fetchone()
                except sqlite3.OperationalError as exc:
                    raise ValueError("候选规则尚未完成测试") from exc
                if not detail or not validation or not job or job["status"] != "completed" or job["result_id"] != validation["id"]:
                    raise ValueError("候选规则尚未完成测试")
                try:
                    result = json.loads(validation["result_json"])
                    valid = (isinstance(result, dict) and result.get("passed") is True
                             and result.get("candidate_sha256") == actual
                             and result.get("category") == candidate["category"]
                             and validation["candidate_sha256"] == actual
                             and validation["base_generation"] == generation
                             and result.get("exit_code") == 0
                             and isinstance(result.get("collected_tests"), int)
                             and not isinstance(result.get("collected_tests"), bool)
                             and result["collected_tests"] > 0
                             and result.get("passed_tests") == result["collected_tests"]
                             and result.get("skipped_tests") == 0
                             and not result.get("failed_test_ids"))
                except (TypeError, ValueError, KeyError):
                    valid = False
                if not valid:
                    raise ValueError("候选规则最新测试未通过或不完整")
                if result.get("input_manifest_sha256") != current_manifest:
                    raise ValueError("测试环境已变化，请重新测试")
                if expected_validation_id != validation["id"]:
                    raise ValueError("测试结果已变化，请重新查看后审核")
                validation_id = validation["id"]
            previous = conn.execute("SELECT * FROM rule_candidate_reviews WHERE candidate_id=? ORDER BY rowid DESC LIMIT 1", (candidate_id,)).fetchone()
            if previous and (not self.versioned or previous["decision"] == "reject"
                             or (decision == "approve" and previous["validation_id"] == validation_id)):
                raise ValueError("该候选规则已审核，请提交新候选或先运行新测试")
            review_id, now = uuid.uuid4().hex, time.time()
            self_review = actor["id"] == candidate["submitted_by"]
            conn.execute("INSERT INTO rule_candidate_reviews VALUES (?,?,?,?,?,?,?,?,?)", (
                review_id, candidate_id, actual, actor["id"], decision, reason.strip(), int(self_review), validation_id, now,
            ))
            return {"id": review_id, "candidate_id": candidate_id, "candidate_sha256": actual,
                    "reviewer_id": actor["id"], "decision": decision, "reason": reason.strip(),
                    "self_review": self_review, "validation_id": validation_id, "created_at": now}

    def get(self, token: str, candidate_id: str, *, verify_environment: bool = True) -> dict[str, Any]:
        environment_check = None
        with closing(self.accounts._connect()) as conn:
            conn.execute("BEGIN")
            actor = self.accounts._session(conn, token)
            candidate = conn.execute("SELECT * FROM rule_candidates WHERE id=?", (candidate_id,)).fetchone()
            if candidate is None:
                raise ValueError("候选规则不存在")
            if actor["role"] != "admin" and candidate["submitted_by"] != actor["id"]:
                raise AuthorizationError("无权查看该候选规则")
            if (hashlib.sha256(candidate["content"]).hexdigest() != candidate["sha256"]
                    or hashlib.sha256(candidate["baseline"]).hexdigest() != candidate["base_sha256"]):
                raise ValueError("候选规则内容损坏")
            result = {key: candidate[key] for key in ("id", "category", "sha256", "base_sha256", "base_generation", "submitted_by", "created_at")}
            result["content"] = candidate["content"]
            result["baseline"] = candidate["baseline"]
            row = conn.execute("SELECT * FROM rule_candidate_reviews WHERE candidate_id=? ORDER BY rowid DESC LIMIT 1", (candidate_id,)).fetchone()
            if row:
                review = dict(row)
                review["self_review"] = bool(row["self_review"])
                result["review"] = review
            else:
                result["review"] = None
            if self.versioned:
                try:
                    current, generation = self._baseline(conn, candidate["category"])
                    if generation != candidate["base_generation"] or hashlib.sha256(current).hexdigest() != candidate["base_sha256"]:
                        result["status"] = "基线已变化"
                    elif row and row["decision"] == "reject":
                        result["status"] = "已退回"
                    else:
                        job = conn.execute("SELECT * FROM rule_validation_jobs WHERE candidate_id=? ORDER BY rowid DESC LIMIT 1", (candidate_id,)).fetchone()
                        validation = conn.execute("SELECT * FROM rule_validation_results WHERE candidate_id=? ORDER BY rowid DESC LIMIT 1", (candidate_id,)).fetchone()
                        if not job:
                            result["status"] = "待测试"
                        elif job["status"] == "running":
                            result["status"] = "测试已中断，请检查后恢复" if job["lease_until"] <= time.time() else "测试中"
                        elif job["status"] != "completed" or not validation or job["result_id"] != validation["id"]:
                            result["status"] = "测试未完成"
                        elif json.loads(validation["result_json"]).get("passed") is not True:
                            result["status"] = ("测试已中断，请检查后恢复" if json.loads(validation["result_json"]).get("exit_code") is None else "测试未通过")
                        elif not verify_environment:
                            result["status"] = "待核验测试环境"
                        else:
                            environment_check = {
                                "category": candidate["category"], "content": candidate["content"],
                                "sha256": candidate["sha256"], "base_sha256": candidate["base_sha256"],
                                "generation": generation, "manifest": json.loads(validation["result_json"]).get("input_manifest_sha256"),
                                "job_id": job["id"], "validation_id": validation["id"],
                                "review_id": row["id"] if row else None,
                                "approved": bool(row and row["decision"] == "approve" and row["validation_id"] == validation["id"]),
                            }
                            result["status"] = "待核验测试环境"
                except (sqlite3.OperationalError, ValueError, TypeError, AttributeError, OSError):
                    result["status"] = "测试或基线不可用"
            else:
                result["status"] = "本机历史记录"
        if environment_check is not None:
            try:
                snapshot = RuleSnapshot(environment_check["category"], environment_check["content"], environment_check["sha256"][:12])
                manifest = current_input_manifest(snapshot)
            except (ValueError, TypeError, OSError):
                manifest = None
            with closing(self.accounts._connect()) as conn:
                conn.execute("BEGIN")
                actor = self.accounts._session(conn, token)
                current_candidate = conn.execute("SELECT * FROM rule_candidates WHERE id=?", (candidate_id,)).fetchone()
                if current_candidate is None:
                    raise ValueError("候选规则不存在")
                if actor["role"] != "admin" and current_candidate["submitted_by"] != actor["id"]:
                    raise AuthorizationError("无权查看该候选规则")
                latest_job = conn.execute("SELECT * FROM rule_validation_jobs WHERE candidate_id=? ORDER BY rowid DESC LIMIT 1", (candidate_id,)).fetchone()
                latest_validation = conn.execute("SELECT id FROM rule_validation_results WHERE candidate_id=? ORDER BY rowid DESC LIMIT 1", (candidate_id,)).fetchone()
                latest_review = conn.execute("SELECT id FROM rule_candidate_reviews WHERE candidate_id=? ORDER BY rowid DESC LIMIT 1", (candidate_id,)).fetchone()
                baseline, generation = self._baseline(conn, environment_check["category"])
                unchanged = (current_candidate["sha256"] == environment_check["sha256"]
                             and hashlib.sha256(current_candidate["content"]).hexdigest() == environment_check["sha256"]
                             and generation == environment_check["generation"]
                             and hashlib.sha256(baseline).hexdigest() == environment_check["base_sha256"]
                             and latest_job is not None and latest_job["id"] == environment_check["job_id"]
                             and latest_job["status"] == "completed"
                             and latest_job["result_id"] == environment_check["validation_id"]
                             and latest_validation is not None and latest_validation["id"] == environment_check["validation_id"]
                             and (latest_review["id"] if latest_review else None) == environment_check["review_id"])
                if not unchanged:
                    result["status"] = "状态已变化，请刷新"
                elif manifest is None:
                    result["status"] = "测试或基线不可用"
                elif manifest != environment_check["manifest"]:
                    result["status"] = "测试环境已变化"
                else:
                    result["status"] = "已审核" if environment_check["approved"] else "待审核"
        return result
