"""Experimental offline copy into a new same-database file; not wired to HTTP.

Call only with all old processes stopped. The caller is responsible for enforcing
the maintenance window; separate backups cannot prove cross-database consistency.
Do not use with real data until independent review and recovery tests are complete.
"""
from __future__ import annotations

import hashlib
import ctypes
import errno
import json
import os
import re
import site
import sqlite3
import subprocess
import sys
import sysconfig
import tempfile
import time
from contextlib import ExitStack, closing, contextmanager
from pathlib import Path
from typing import Any

from app.services.migration_inspection import _ACCOUNT_TABLES, _REVIEW_TABLES, _snapshot

_ROOT = Path(__file__).resolve().parents[2]
_DATABASE_FILENAME = "agent-t.sqlite"
_REFERENCE_SCRIPT = r'''
import json, sqlite3, sys
from pathlib import Path
sys.path[:0] = json.loads(sys.argv[2])
from app.services.account_store import AccountStore
from app.services.rule_release import RuleReleaseService
from app.services.rule_validation_service import RuleValidationService
from app.services import ai_findings, impact, llm_call_log, remediation_budget, revision
accounts = AccountStore(Path(sys.argv[1]))
RuleReleaseService(accounts)
RuleValidationService(accounts)
with revision._connect() as conn:
    conn.execute(impact._SELECTED_DDL)
    conn.execute(impact._IMPACT_REPORTS_DDL)
    conn.execute(ai_findings._STATUS_DDL)
    remediation_budget._create(conn)
    llm_call_log._ensure_table(conn)
'''


def _reference(account: Path, review: Path, temp: Path) -> None:
    env = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP") if key in os.environ}
    package_paths = {sysconfig.get_path("purelib"), sysconfig.get_path("platlib"), site.getusersitepackages()}
    imports = [str(_ROOT)] + sorted(str(path) for path in package_paths if path and Path(path).is_dir())
    env.update(STORE_DB_PATH=str(review), PYTHONPATH=os.pathsep.join(imports), PYTHONUTF8="1",
               PYTHONPYCACHEPREFIX=str(temp / "pycache"), PYTHONNOUSERSITE="1")
    result = subprocess.run([sys.executable, "-I", "-S", "-B", "-c", _REFERENCE_SCRIPT, str(account), json.dumps(imports)], cwd=_ROOT,
                            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60, check=False)
    if result.returncode != 0:
        raise RuntimeError("可信数据库结构生成失败")


def _open(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    conn.execute("PRAGMA trusted_schema=OFF")
    conn.execute("PRAGMA query_only=ON")
    return conn


def _objects(conn: sqlite3.Connection) -> dict[tuple[str, str], tuple[str, str | None]]:
    return {(kind, name): (table, sql) for kind, name, table, sql in conn.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_master"
    )}


def _check_schema(source: sqlite3.Connection, reference: sqlite3.Connection,
                  allowed: set[str], required: set[str]) -> dict[tuple[str, str], tuple[str, str | None]]:
    actual, trusted = _objects(source), _objects(reference)
    tables = {name for kind, name in actual if kind == "table"}
    if not required.issubset(tables) or not tables.issubset(allowed):
        raise ValueError("源数据库包含未知表或缺少必要表")
    for key, value in actual.items():
        if key[0] not in {"table", "index", "trigger"} or key not in trusted or trusted[key] != value:
            raise ValueError("源数据库结构不受支持")
    if any(key not in actual for key, (table, _sql) in trusted.items() if table in tables):
        raise ValueError("源数据库缺少必要的索引或触发器")
    if source.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
        raise ValueError("源数据库完整性检查失败")
    return actual


def _hash_rows(rows: Any) -> tuple[int, str]:
    digest = hashlib.sha256()
    count = 0
    for row in rows:
        values = [["blob", value.hex()] if isinstance(value, bytes) else [type(value).__name__, value] for value in row]
        encoded = json.dumps(values, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big") + encoded)
        count += 1
    return count, digest.hexdigest()


@contextmanager
def _lock_sources(account: Path, review: Path):
    """Block SQLite writers on both sources until the copy is published."""
    with ExitStack() as stack:
        for path in sorted((account, review), key=lambda item: str(item).casefold()):
            conn = stack.enter_context(closing(sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=1)))
            conn.execute("BEGIN IMMEDIATE")
        yield


def _publish_directory(source: Path, target: Path) -> None:
    """Atomically publish a complete directory without replacing an existing one."""
    with os.scandir(source) as iterator:
        entries = list(iterator)
    if (len(entries) != 1 or entries[0].name != _DATABASE_FILENAME or
            not entries[0].is_file(follow_symlinks=False)):
        raise ValueError("迁移暂存目录包含未预期文件")
    if os.name == "nt":
        os.rename(source, target)  # Windows rename fails when the destination exists.
        return
    if sys.platform.startswith("linux"):
        libc = ctypes.CDLL(None, use_errno=True)
        renameat2 = getattr(libc, "renameat2", None)
        if renameat2 is None:
            raise OSError(errno.ENOTSUP, "当前平台缺少安全的目录发布能力")
        renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        renameat2.restype = ctypes.c_int
        if renameat2(-100, os.fsencode(source), -100, os.fsencode(target), 1) != 0:
            code = ctypes.get_errno()
            raise OSError(code, os.strerror(code), str(target))
        return
    raise OSError(errno.ENOTSUP, "当前平台不支持安全的目录发布")


def create_migrated_copy(account_path: Path, review_path: Path, target_path: Path, *, maintenance_confirmed: bool) -> dict[str, Any]:
    """Create a new directory containing the copy; never switch app configuration.

    SQLite writes are locked during copying, but the boolean is an operator
    assertion and old processes may resume writing after the locks are released.
    Temporary full copies contain sensitive data and may remain after a crash.
    This experimental helper is not a production migration or isolation gate.
    """
    if maintenance_confirmed is not True:
        raise ValueError("需要停服维护窗口")
    account, review, target = (Path(p) for p in (account_path, review_path, target_path))
    if not account.is_absolute() or not review.is_absolute() or not target.is_absolute():
        raise ValueError("数据库路径必须为绝对路径")
    if not account.is_file() or not review.is_file() or not target.parent.is_dir():
        raise ValueError("源数据库或目标目录不存在")
    account, review = account.resolve(), review.resolve()
    target = target.parent.resolve() / target.name
    if account == review or os.path.samefile(account, review) or target in {account, review}:
        raise ValueError("源库与目标必须不同")
    if os.path.lexists(target):
        raise FileExistsError("目标目录已存在")
    with _lock_sources(account, review), tempfile.TemporaryDirectory(prefix="agent-t-migrate-", dir=target.parent) as temp_text:
        temp = Path(temp_text)
        account_snapshot, review_snapshot = temp / "a.sqlite", temp / "r.sqlite"
        reference_account, reference_review = temp / "ra.sqlite", temp / "rr.sqlite"
        package = temp / "package"
        package.mkdir()
        out = package / _DATABASE_FILENAME
        _snapshot(account, account_snapshot, time.monotonic() + 30)
        _snapshot(review, review_snapshot, time.monotonic() + 30)
        _reference(reference_account, reference_review, temp)
        with closing(_open(account_snapshot)) as ac, closing(_open(review_snapshot)) as rc, \
             closing(_open(reference_account)) as ar, closing(_open(reference_review)) as rr:
            ao = _check_schema(ac, ar, _ACCOUNT_TABLES, {"account_users", "account_sessions"})
            ro = _check_schema(rc, rr, _REVIEW_TABLES, {"reviews"})
            # codex 二轮 P1-4：验证三表（控制/任务/结果）必须**全有或全无**——
            # 源库部分缺失（如控制表丢失而任务表仍在=风险闸状态已不完整）时
            # 拒绝迁移：否则副本在目标库重建 blocked=0 的控制表，会把受阻的
            # 安全闸恢复为可用（安全闸风险）。损坏库走离线迁移，不入副本。
            _validation_tables = {"rule_validation_control", "rule_validation_jobs",
                                  "rule_validation_results"} & {
                name for kind, name in ao if kind == "table"}
            if 0 < len(_validation_tables) < 3:
                raise ValueError("规则验证控制/任务/结果表不完整，源库需离线迁移")
            if "rule_validation_control" in _validation_tables:
                # codex 三轮 P1-2：三表齐全但控制行（id=1）丢失同样拒绝——
                # 目标库初始化会补出默认未阻断行，等同复活安全闸
                if ac.execute("SELECT 1 FROM rule_validation_control WHERE id=1").fetchone() is None:
                    raise ValueError("规则验证控制行缺失，源库需离线迁移")
            if ac.execute("PRAGMA foreign_key_check").fetchone():
                raise ValueError("账户或规则关联无效")
            if ("table", "rule_versions") in ao:
                for raw, digest in ac.execute("SELECT content, sha256 FROM rule_versions"):
                    if not isinstance(raw, bytes) or hashlib.sha256(raw).hexdigest() != digest:
                        raise ValueError("规则版本摘要无效")
            if ("table", "rule_candidates") in ao:
                for content, digest, baseline, base_digest in ac.execute("SELECT content,sha256,baseline,base_sha256 FROM rule_candidates"):
                    if (not isinstance(content, bytes) or not isinstance(baseline, bytes) or
                            hashlib.sha256(content).hexdigest() != digest or hashlib.sha256(baseline).hexdigest() != base_digest):
                        raise ValueError("候选规则摘要无效")
            if ("table", "rule_candidate_reviews") in ao and ac.execute(
                "SELECT 1 FROM rule_candidate_reviews r JOIN rule_candidates c ON c.id=r.candidate_id "
                "WHERE r.candidate_sha256 != c.sha256 OR r.self_review != (r.reviewer_id=c.submitted_by) "
                "OR (c.base_generation IS NOT NULL AND r.decision='approve' AND r.validation_id IS NULL) LIMIT 1"
            ).fetchone():
                raise ValueError("规则审核与候选不一致")
            if ("table", "rule_validation_results") in ao and ac.execute(
                "SELECT 1 FROM rule_validation_results r JOIN rule_candidates c ON c.id=r.candidate_id "
                "WHERE r.candidate_sha256 != c.sha256 OR r.base_generation IS NOT c.base_generation LIMIT 1"
            ).fetchone():
                raise ValueError("规则测试与候选不一致")
            if ("table", "rule_validation_results") in ao:
                for digest, category, result_json in ac.execute(
                    "SELECT v.candidate_sha256,c.category,v.result_json FROM rule_validation_results v "
                    "JOIN rule_candidates c ON c.id=v.candidate_id"
                ):
                    try:
                        result = json.loads(result_json)
                        if (not isinstance(result, dict) or result.get("candidate_sha256") != digest or
                                result.get("category") != category or not isinstance(result.get("passed"), bool) or
                                not isinstance(result.get("failed_test_ids"), list) or
                                not all(isinstance(item, str) for item in result["failed_test_ids"]) or
                                not re.fullmatch(r"[0-9a-f]{64}", str(result.get("input_manifest_sha256") or ""))):
                            raise ValueError("规则测试证据无效")
                        collected = result.get("collected_tests")
                        if result.get("passed") is True and (
                            type(result.get("exit_code")) is not int or result.get("exit_code") != 0 or
                            type(collected) is not int or collected <= 0 or type(result.get("passed_tests")) is not int or
                            result.get("passed_tests") != collected or type(result.get("skipped_tests")) is not int or
                            result.get("skipped_tests") != 0 or bool(result.get("failed_test_ids"))
                        ):
                            raise ValueError("规则测试通过证据无效")
                    except (TypeError, json.JSONDecodeError) as exc:
                        raise ValueError("规则测试证据无效") from exc
            if ("table", "rule_validation_jobs") in ao and ("table", "rule_validation_results") in ao and ac.execute(
                "SELECT 1 FROM rule_validation_jobs j LEFT JOIN rule_validation_results v ON v.id=j.result_id "
                "WHERE (j.status='completed' AND (v.id IS NULL OR v.candidate_id != j.candidate_id OR v.actor_id != j.actor_id)) "
                "OR (j.status!='completed' AND j.result_id IS NOT NULL) LIMIT 1"
            ).fetchone():
                raise ValueError("规则测试任务与结果不一致")
            if ("table", "rule_validation_jobs") in ao and ("table", "rule_validation_results") not in ao and ac.execute(
                "SELECT 1 FROM rule_validation_jobs WHERE status='completed' OR result_id IS NOT NULL LIMIT 1"
            ).fetchone():
                raise ValueError("规则测试任务与结果不一致")
            if ("table", "rule_candidate_reviews") in ao and ("table", "rule_validation_results") in ao and ac.execute(
                "SELECT 1 FROM rule_candidate_reviews r LEFT JOIN rule_validation_results v ON v.id=r.validation_id "
                "WHERE r.validation_id IS NOT NULL AND (v.id IS NULL OR v.candidate_id != r.candidate_id "
                "OR v.candidate_sha256 != r.candidate_sha256) LIMIT 1"
            ).fetchone():
                raise ValueError("规则审核与测试不一致")
            if ("table", "rule_candidate_reviews") in ao and ("table", "rule_validation_results") in ao:
                for (result_json,) in ac.execute(
                    "SELECT v.result_json FROM rule_candidate_reviews r JOIN rule_validation_results v ON v.id=r.validation_id "
                    "WHERE r.decision='approve'"
                ):
                    if json.loads(result_json).get("passed") is not True:
                        raise ValueError("通过审核的规则测试未通过")
            if ("table", "rule_candidate_reviews") in ao and ("table", "rule_validation_results") not in ao and ac.execute(
                "SELECT 1 FROM rule_candidate_reviews WHERE validation_id IS NOT NULL LIMIT 1"
            ).fetchone():
                raise ValueError("规则审核与测试不一致")
            if ("table", "rule_active_versions") in ao and ac.execute(
                "SELECT 1 FROM rule_active_versions a LEFT JOIN rule_versions v ON v.id=a.version_id "
                "WHERE v.id IS NULL OR v.category != a.category LIMIT 1"
            ).fetchone():
                raise ValueError("规则活动版本无效")
            if ("table", "rule_validation_jobs") in ao and ac.execute("SELECT 1 FROM rule_validation_jobs WHERE status='running' LIMIT 1").fetchone():
                raise ValueError("存在进行中的规则测试")
            for review_id, data in rc.execute("SELECT id, data FROM reviews"):
                try:
                    row = json.loads(data)
                    if not isinstance(row, dict) or row.get("id") != review_id:
                        raise ValueError("合同记录身份无效")
                    turns = (row.get("ask_thread") or {}).get("turns") or []
                    if row.get("status") == "processing" or any(isinstance(turn, dict) and turn.get("state") == "processing" for turn in turns):
                        raise ValueError("存在进行中的合同任务")
                except (TypeError, AttributeError, json.JSONDecodeError) as exc:
                    raise ValueError("合同记录结构无效") from exc
            for table in _REVIEW_TABLES - {"reviews"}:
                if ("table", table) in ro and rc.execute(
                    f'SELECT 1 FROM "{table}" c LEFT JOIN reviews r ON r.id=c.review_id '
                    "WHERE c.review_id IS NOT NULL AND r.id IS NULL LIMIT 1"
                ).fetchone():
                    raise ValueError("合同关联记录包含孤儿数据")
            if ("table", "selected_drafts") in ro and (
                (("table", "revision_drafts") not in ro and rc.execute("SELECT 1 FROM selected_drafts LIMIT 1").fetchone()) or
                (("table", "revision_drafts") in ro and rc.execute(
                    "SELECT 1 FROM selected_drafts s LEFT JOIN revision_drafts d ON d.id=s.draft_id "
                    "WHERE d.id IS NULL OR d.review_id != s.review_id OR d.item_id != s.item_id LIMIT 1"
                ).fetchone())
            ):
                raise ValueError("合同修订选择与草稿不一致")
            if ("table", "impact_reports") in ro and (
                (("table", "revision_drafts") not in ro and rc.execute("SELECT 1 FROM impact_reports LIMIT 1").fetchone()) or
                (("table", "revision_drafts") in ro and rc.execute(
                    "SELECT 1 FROM impact_reports i LEFT JOIN revision_drafts d ON d.id=i.draft_id "
                    "WHERE d.id IS NULL OR d.review_id != i.review_id OR d.item_id != i.item_id LIMIT 1"
                ).fetchone())
            ):
                raise ValueError("合同影响报告与草稿不一致")
            if ("table", "remediation_calls") in ro and rc.execute("SELECT 1 FROM remediation_calls WHERE state='processing' LIMIT 1").fetchone():
                raise ValueError("存在进行中的合同任务")
            all_objects = {**ao, **ro}
            if len(all_objects) != len(ao) + len(ro):
                raise ValueError("账户库与合同库结构冲突")
            with closing(sqlite3.connect(out)) as dest, dest:
                dest.execute("PRAGMA foreign_keys=OFF")
                dest.execute("BEGIN IMMEDIATE")
                for kind in ("table", "index"):
                    for (obj_kind, _), (_, sql) in all_objects.items():
                        if obj_kind == kind and sql:
                            dest.execute(sql)
                for source, objects in ((ac, ao), (rc, ro)):
                    for kind, name in objects:
                        if kind != "table" or name == "account_sessions":
                            continue
                        columns = [row[1] for row in source.execute(f'PRAGMA table_info("{name}")')]
                        quoted = ",".join('"' + col.replace('"', '""') + '"' for col in columns)
                        select = f'SELECT rowid,{quoted} FROM "{name}" ORDER BY rowid'
                        insert = f'INSERT INTO "{name}" (rowid,{quoted}) VALUES (' + ",".join("?" for _ in range(len(columns) + 1)) + ")"
                        dest.executemany(insert, source.execute(select))
                        if _hash_rows(source.execute(select)) != _hash_rows(dest.execute(select)):
                            raise ValueError("迁移数据核对失败")
                dest.execute("ALTER TABLE reviews ADD COLUMN owner_user_id TEXT")
                dest.execute("CREATE INDEX idx_reviews_owner_created ON reviews(owner_user_id, created_at DESC)")
                for (kind, _), (_, sql) in all_objects.items():
                    if kind == "trigger" and sql:
                        dest.execute(sql)
                if dest.execute("PRAGMA integrity_check").fetchall() != [("ok",)] or dest.execute("PRAGMA foreign_key_check").fetchone():
                    raise ValueError("迁移结果完整性校验失败")
            _publish_directory(package, target)
    return {"created": True, "database_filename": _DATABASE_FILENAME, "old_sessions_copied": False, "legacy_reviews_owned": False,
            "maintenance_enforced": False, "contract_access_integrated": False}
