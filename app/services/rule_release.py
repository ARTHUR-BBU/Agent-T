"""Internal atomic version switch; no API or complete-regression producer yet.

Do not expose as a release feature until full evidence, baseline certification,
immutable builds, execution-source integration and contract isolation exist.
"""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import re
import sqlite3
import stat
import sys
import time
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any

from app.services.account_store import AccountStore
from app.services.rule_candidate_validation import current_input_manifest
from app.services.rule_snapshot import RuleSnapshot
from app.services.rule_validation_service import RuleValidationService
from app.services.rule_validation_guard import require_validation_ready

_ROOT = Path(__file__).resolve().parents[2]
_MANIFEST_MAX_FILES = 20_000
_MANIFEST_MAX_BYTES = 512 * 1024 * 1024


def _full_manifest_paths(base: Path, *, max_files: int | None = None) -> list[Path]:
    paths = []
    def add(path: Path) -> None:
        paths.append(path)
        if max_files is not None and len(paths) > max_files:
            raise RuntimeError("回归输入文件数量超限")
    ignored_dirs = {"__pycache__", ".pytest_cache", ".ruff_cache", ".git"}
    for name in ("app", "config", "tests", "fixtures", "fixtures-real", "fixtures-tisvc",
                 "fixtures-classification", "scripts", "tools"):
        root = base / name
        if root.is_symlink() or getattr(root, "is_junction", lambda: False)():
            raise RuntimeError("回归输入目录不能是链接")
        if root.exists():
            if not stat.S_ISDIR(root.stat(follow_symlinks=False).st_mode):
                raise RuntimeError("回归输入目录无效")
            def fail_walk(error: OSError) -> None:
                raise error

            for current, dirs, files in os.walk(root, followlinks=False, onerror=fail_walk):
                dirs[:] = [entry for entry in dirs if entry not in ignored_dirs]
                for entry in dirs:
                    directory = Path(current) / entry
                    if directory.is_symlink() or getattr(directory, "is_junction", lambda: False)():
                        raise RuntimeError("回归输入目录不能包含链接")
                for entry in files:
                    path = Path(current) / entry
                    if entry.endswith((".pyc", ".pyo")) or entry == ".env" or entry.startswith(".env."):
                        continue
                    if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
                        raise RuntimeError("回归输入文件不能是链接")
                    if not stat.S_ISREG(path.stat(follow_symlinks=False).st_mode):
                        raise RuntimeError("回归输入包含非普通文件")
                    add(path)
    for name in ("pyproject.toml", "pytest.ini", ".pytest.ini", "tox.ini", "setup.cfg", "requirements.txt",
                 "requirements-dev.txt", "requirements-lock.txt", "requirements-hf.txt", "README.md",
                 "README_EN.md", "Dockerfile", "Dockerfile.server"):
        path = base / name
        if path.is_symlink():
            raise RuntimeError("回归输入文件不能是链接")
        if path.exists():
            if not stat.S_ISREG(path.stat(follow_symlinks=False).st_mode):
                raise RuntimeError("回归输入包含非普通文件")
            add(path)
    return sorted(paths)


def _full_manifest_at(base: Path, *, max_files: int | None = None, max_bytes: int | None = None) -> str:
    digest = hashlib.sha256()
    env = {"python": sys.version, "packages": sorted((d.metadata["Name"] or "", d.version)
                                                    for d in importlib.metadata.distributions())}
    digest.update(json.dumps(env, sort_keys=True).encode("utf-8"))
    total = 0
    for path in _full_manifest_paths(base, max_files=max_files):
        name = path.relative_to(base).as_posix().encode("utf-8")
        size = path.stat(follow_symlinks=False).st_size
        if max_bytes is not None and total + size > max_bytes:
            raise RuntimeError("回归输入大小超限")
        digest.update(len(name).to_bytes(8, "big") + name + size.to_bytes(8, "big"))
        seen = 0
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                seen += len(chunk)
                if seen > size or (max_bytes is not None and total + seen > max_bytes):
                    raise RuntimeError("回归输入在读取期间发生变化")
                digest.update(chunk)
        if seen != size:
            raise RuntimeError("回归输入在读取期间发生变化")
        total += seen
    return digest.hexdigest()


def current_full_manifest() -> str:
    """Best-effort local source fingerprint; not a signed immutable build."""
    return _full_manifest_at(_ROOT, max_files=_MANIFEST_MAX_FILES, max_bytes=_MANIFEST_MAX_BYTES)


class RuleReleaseService:
    def __init__(self, accounts: AccountStore) -> None:
        self.accounts = accounts
        RuleValidationService(accounts)
        with closing(accounts._connect()) as conn, conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS rule_full_regression_results (
                    id TEXT PRIMARY KEY, candidate_id TEXT NOT NULL REFERENCES rule_candidates(id),
                    candidate_sha256 TEXT NOT NULL, base_generation INTEGER NOT NULL,
                    input_manifest_sha256 TEXT NOT NULL, passed INTEGER NOT NULL CHECK(passed IN (0,1)),
                    scope TEXT NOT NULL, created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS rule_release_events (
                    id TEXT PRIMARY KEY, category TEXT NOT NULL, action TEXT NOT NULL,
                    previous_version_id TEXT NOT NULL, version_id TEXT NOT NULL,
                    previous_generation INTEGER NOT NULL, generation INTEGER NOT NULL,
                    actor_id TEXT NOT NULL REFERENCES account_users(id), request_id TEXT NOT NULL,
                    fingerprint TEXT NOT NULL, candidate_id TEXT, validation_id TEXT, review_id TEXT,
                    full_result_id TEXT, reason TEXT NOT NULL, created_at REAL NOT NULL,
                    UNIQUE(actor_id, request_id)
                );
                CREATE TRIGGER IF NOT EXISTS full_regression_no_update BEFORE UPDATE ON rule_full_regression_results
                BEGIN SELECT RAISE(ABORT, 'full regression results are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS full_regression_no_delete BEFORE DELETE ON rule_full_regression_results
                BEGIN SELECT RAISE(ABORT, 'full regression results are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS full_regression_no_replace BEFORE INSERT ON rule_full_regression_results
                WHEN EXISTS (SELECT 1 FROM rule_full_regression_results WHERE id=NEW.id)
                BEGIN SELECT RAISE(ABORT, 'full regression results are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS release_event_no_update BEFORE UPDATE ON rule_release_events
                BEGIN SELECT RAISE(ABORT, 'release events are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS release_event_no_delete BEFORE DELETE ON rule_release_events
                BEGIN SELECT RAISE(ABORT, 'release events are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS release_event_no_replace BEFORE INSERT ON rule_release_events
                WHEN EXISTS (SELECT 1 FROM rule_release_events WHERE id=NEW.id OR
                             (actor_id=NEW.actor_id AND request_id=NEW.request_id))
                BEGIN SELECT RAISE(ABORT, 'release events are append-only'); END;
            """)

    @staticmethod
    def _arguments(generation: int, request_id: str) -> None:
        if not isinstance(generation, int) or isinstance(generation, bool) or generation < 1:
            raise ValueError("规则基线代次无效")
        if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", request_id):
            raise ValueError("请求编号无效")

    @staticmethod
    def _existing(conn: sqlite3.Connection, actor_id: str, request_id: str, fingerprint: str) -> dict[str, Any] | None:
        row = conn.execute("SELECT * FROM rule_release_events WHERE actor_id=? AND request_id=?", (actor_id, request_id)).fetchone()
        if row is None:
            return None
        if row["fingerprint"] != fingerprint:
            raise ValueError("请求编号已用于不同操作")
        return {key: row[key] for key in ("id", "category", "action", "previous_version_id", "version_id", "previous_generation", "generation")}

    def publish(self, token: str, candidate_id: str, *, expected_generation: int, validation_id: str,
                review_id: str, full_result_id: str, request_id: str,
                expected_candidate_sha256: str | None = None) -> dict[str, Any]:
        self._arguments(expected_generation, request_id)
        if expected_candidate_sha256 is not None and not re.fullmatch(r"[a-f0-9]{64}", expected_candidate_sha256):
            raise ValueError("候选规则摘要无效")
        fingerprint = hashlib.sha256(json.dumps(["publish", candidate_id, expected_generation, validation_id, review_id, full_result_id, expected_candidate_sha256]).encode()).hexdigest()
        with closing(self.accounts._connect()) as conn:
            conn.execute("BEGIN")
            actor = self.accounts._session(conn, token, admin=True)
            existing = self._existing(conn, actor["id"], request_id, fingerprint)
            if existing is not None:
                return existing
            candidate = conn.execute("SELECT * FROM rule_candidates WHERE id=?", (candidate_id,)).fetchone()
            if candidate is None:
                raise ValueError("候选规则不存在")
            digest = hashlib.sha256(candidate["content"]).hexdigest()
            if digest != candidate["sha256"] or (expected_candidate_sha256 is not None and digest != expected_candidate_sha256):
                raise ValueError("候选规则内容损坏")
            snapshot = RuleSnapshot(candidate["category"], candidate["content"], digest[:12])
        gold_manifest = current_input_manifest(snapshot)
        full_manifest = current_full_manifest()
        with closing(self.accounts._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            actor = self.accounts._session(conn, token, admin=True)
            existing = self._existing(conn, actor["id"], request_id, fingerprint)
            if existing is not None:
                return existing
            require_validation_ready(conn)
            candidate = conn.execute("SELECT c.* FROM rule_candidates c JOIN rule_candidate_details d ON d.candidate_id=c.id WHERE c.id=?", (candidate_id,)).fetchone()
            if candidate is None or hashlib.sha256(candidate["content"]).hexdigest() != digest or candidate["sha256"] != digest:
                raise ValueError("候选规则内容已变化")
            active = conn.execute("SELECT a.*, v.sha256 FROM rule_active_versions a JOIN rule_versions v ON v.id=a.version_id AND v.category=a.category WHERE a.category=?", (candidate["category"],)).fetchone()
            if (active is None or active["generation"] != expected_generation or
                    candidate["base_generation"] != expected_generation or active["sha256"] != candidate["base_sha256"]):
                raise ValueError("规则基线代次已变化")
            job = conn.execute("SELECT * FROM rule_validation_jobs WHERE candidate_id=? ORDER BY rowid DESC LIMIT 1", (candidate_id,)).fetchone()
            validation = conn.execute("SELECT * FROM rule_validation_results WHERE candidate_id=? ORDER BY rowid DESC LIMIT 1", (candidate_id,)).fetchone()
            review = conn.execute("SELECT * FROM rule_candidate_reviews WHERE candidate_id=? ORDER BY rowid DESC LIMIT 1", (candidate_id,)).fetchone()
            if (job is None or job["status"] != "completed" or job["result_id"] != validation_id or
                    validation is None or validation["id"] != validation_id or validation["candidate_sha256"] != digest or
                    validation["base_generation"] != expected_generation or review is None or review["id"] != review_id or
                    review["decision"] != "approve" or review["candidate_sha256"] != digest or review["validation_id"] != validation_id):
                raise ValueError("规则测试或审核已失效")
            try:
                evidence = json.loads(validation["result_json"])
                gold_ok = (evidence.get("passed") is True and evidence.get("candidate_sha256") == digest and
                           evidence.get("input_manifest_sha256") == gold_manifest and evidence.get("exit_code") == 0 and
                           isinstance(evidence.get("collected_tests"), int) and not isinstance(evidence.get("collected_tests"), bool) and
                           evidence["collected_tests"] > 0 and evidence.get("passed_tests") == evidence["collected_tests"] and
                           evidence.get("skipped_tests") == 0 and not evidence.get("failed_test_ids"))
            except (ValueError, TypeError, AttributeError, KeyError):
                gold_ok = False
            if not gold_ok:
                raise ValueError("规则金标测试已失效")
            full = conn.execute("SELECT * FROM rule_full_regression_results WHERE candidate_id=? ORDER BY rowid DESC LIMIT 1", (candidate_id,)).fetchone()
            if (full is None or full["id"] != full_result_id or full["candidate_sha256"] != digest or
                    full["base_generation"] != expected_generation or full["input_manifest_sha256"] != full_manifest or
                    full["passed"] != 1 or full["scope"] != "complete_candidate_regression"):
                raise ValueError("候选规则尚未通过完整回归")
            version = conn.execute("SELECT * FROM rule_versions WHERE category=? AND sha256=?", (candidate["category"], digest)).fetchone()
            if version is None:
                version_id = uuid.uuid4().hex
                # 施工卡-20261114-2：INSERT 一律写明确列名，不再依赖列序
                conn.execute("INSERT INTO rule_versions (id,category,sha256,content,actor_id,created_at) "
                             "VALUES (?,?,?,?,?,?)", (version_id, candidate["category"], digest, candidate["content"], actor["id"], time.time()))
            else:
                if hashlib.sha256(version["content"]).hexdigest() != digest:
                    raise ValueError("规则版本内容损坏")
                version_id = version["id"]
            changed = conn.execute("UPDATE rule_active_versions SET version_id=?, generation=generation+1 WHERE category=? AND version_id=? AND generation=?", (version_id, candidate["category"], active["version_id"], expected_generation)).rowcount
            if changed != 1:
                raise ValueError("规则基线代次已变化")
            event_id = uuid.uuid4().hex
            conn.execute("INSERT INTO rule_release_events "
                         "(id,category,action,previous_version_id,version_id,previous_generation,generation,"
                         "actor_id,request_id,fingerprint,candidate_id,validation_id,review_id,full_result_id,"
                         "reason,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                event_id, candidate["category"], "publish", active["version_id"], version_id,
                expected_generation, expected_generation + 1, actor["id"], request_id, fingerprint,
                candidate_id, validation_id, review_id, full_result_id, "", time.time(),
            ))
            self.accounts._audit(conn, actor["id"], "publish_rule_version", version_id)
            return {"id": event_id, "category": candidate["category"], "action": "publish", "previous_version_id": active["version_id"],
                    "version_id": version_id, "previous_generation": expected_generation, "generation": expected_generation + 1}

    def rollback(self, token: str, category: str, version_id: str, *, expected_generation: int,
                 reason: str, request_id: str) -> dict[str, Any]:
        self._arguments(expected_generation, request_id)
        if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 1000:
            raise ValueError("请填写回滚理由")
        fingerprint = hashlib.sha256(json.dumps(["rollback", category, version_id, expected_generation, reason]).encode()).hexdigest()
        with closing(self.accounts._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            actor = self.accounts._session(conn, token, admin=True)
            existing = self._existing(conn, actor["id"], request_id, fingerprint)
            if existing is not None:
                return existing
            active = conn.execute("SELECT * FROM rule_active_versions WHERE category=?", (category,)).fetchone()
            target = conn.execute("SELECT * FROM rule_versions WHERE id=? AND category=?", (version_id, category)).fetchone()
            if active is None or active["generation"] != expected_generation:
                raise ValueError("规则基线代次已变化")
            if target is None or target["id"] == active["version_id"]:
                raise ValueError("回滚版本无效")
            if conn.execute("SELECT 1 FROM rule_release_events WHERE category=? AND (version_id=? OR previous_version_id=?)", (category, version_id, version_id)).fetchone() is None:
                raise ValueError("目标版本未曾生效")
            digest = hashlib.sha256(target["content"]).hexdigest()
            if digest != target["sha256"]:
                raise ValueError("规则版本内容损坏")
            RuleSnapshot(category, target["content"], digest[:12])
            conn.execute("UPDATE rule_active_versions SET version_id=?, generation=generation+1 WHERE category=? AND generation=?", (version_id, category, expected_generation))
            event_id = uuid.uuid4().hex
            conn.execute("INSERT INTO rule_release_events "
                         "(id,category,action,previous_version_id,version_id,previous_generation,generation,"
                         "actor_id,request_id,fingerprint,candidate_id,validation_id,review_id,full_result_id,"
                         "reason,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                event_id, category, "rollback", active["version_id"], version_id, expected_generation,
                expected_generation + 1, actor["id"], request_id, fingerprint, None, None, None, None, reason.strip(), time.time(),
            ))
            self.accounts._audit(conn, actor["id"], "rollback_rule_version", version_id)
            return {"id": event_id, "category": category, "action": "rollback", "previous_version_id": active["version_id"],
                    "version_id": version_id, "previous_generation": expected_generation, "generation": expected_generation + 1}
