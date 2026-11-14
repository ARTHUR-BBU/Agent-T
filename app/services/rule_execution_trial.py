"""Read-only local experiment for captured database rule versions.

Not a publication gate, production configuration, or contract access control.
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
from contextlib import closing
from pathlib import Path

from app.services.rule_snapshot import RuleSnapshot
from app.services.rule_versions import CapturedRuleVersion


def read_trial_versions(path: Path) -> dict[str, CapturedRuleVersion]:
    if not path.is_absolute() or not path.is_file():
        raise ValueError("试验规则数据库不存在")
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.execute("PRAGMA trusted_schema=OFF")
        conn.execute("BEGIN")
        rows = conn.execute(
            "SELECT a.category,a.version_id,a.generation,v.category AS version_category,v.content,v.sha256 "
            "FROM rule_active_versions a LEFT JOIN rule_versions v ON v.id=a.version_id ORDER BY a.category"
        ).fetchall()
        if not rows or len(rows) > 128:
            raise ValueError("请先初始化试验规则基线")
        result = {}
        for row in rows:
            raw, category, version_id, generation = row["content"], row["category"], row["version_id"], row["generation"]
            if (not isinstance(category, str) or not re.fullmatch(r"[a-z0-9_]{1,64}", category) or
                    row["version_category"] != category or not isinstance(version_id, str) or
                    not re.fullmatch(r"[0-9a-f]{32}", version_id) or type(generation) is not int or generation < 1 or
                    not isinstance(raw, bytes) or not raw or len(raw) > 2_000_000):
                raise ValueError("试验规则版本无效")
            digest = hashlib.sha256(raw).hexdigest()
            if digest != row["sha256"]:
                raise ValueError("试验规则版本摘要不匹配")
            snapshot = RuleSnapshot(category, raw, digest[:12])
            result[category] = CapturedRuleVersion(snapshot, version_id, generation, digest)
        return result
