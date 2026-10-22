# -*- coding: utf-8 -*-
"""收官回测名册纪律测试（Codex P1：名册机器取数+集数漂移必红）。"""
from __future__ import annotations

import json
import pathlib

import pytest

from tools.m65.final_replay import (ARCHIVES, EXPECTED_ROSTER_SIZE,
                                    check_revision, load_roster)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


def _real_roster() -> set[str]:
    names: set[str] = set()
    for rel in ARCHIVES:
        names.update(json.loads((REPO_ROOT / rel).read_bytes()).get("files") or [])
    return names


def test_real_archives_union_is_17() -> None:
    """真实留档并集恰为 17（load_roster 对真实仓库必须通过）。"""
    roster, sources = load_roster(REPO_ROOT)
    assert len(roster) == EXPECTED_ROSTER_SIZE
    assert len(sources) == len(ARCHIVES)
    assert all(len(h) == 16 for h in sources.values())


def test_roster_drift_fails_loud(tmp_path: pathlib.Path) -> None:
    """留档名册变动（少一卷）→ SystemExit，不许悄悄跑旧卷。"""
    assert EXPECTED_ROSTER_SIZE == len(_real_roster())  # 基线自检
    for rel in ARCHIVES:
        data = json.loads((REPO_ROOT / rel).read_bytes())
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        if rel == ARCHIVES[0]:
            # 抽走一卷「仅 run1 独有」的卷——抽两轮共有的卷不会改变并集
            run1_only = "school-uniform-procurement-guangzhou.docx"
            assert run1_only in data["files"]
            data["files"] = [f for f in data["files"] if f != run1_only]
        (tmp_path / rel).write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(SystemExit, match="集数漂移"):
        load_roster(tmp_path)


def test_roster_missing_archive_fails_loud(tmp_path: pathlib.Path) -> None:
    """名册来源缺失 → SystemExit（不静默降级）。"""
    with pytest.raises(SystemExit, match="名册来源缺失"):
        load_roster(tmp_path)


def test_revision_same_commit_short_tag_passes() -> None:
    """同提交短标签（7 位前缀）放行——修复原 40 位 vs 短标签永假比较。"""
    ok, reason = check_revision("ff6b64456c557c7696ae89a1b7f606ce746d2e33", "ff6b644")
    assert ok and reason == ""
    ok, _ = check_revision("FF6B64456C557C7696AE89A1B7F606CE746D2E33", "ff6b644")
    assert ok  # 大小写不敏感


def test_revision_different_sha_rejects() -> None:
    ok, reason = check_revision("ff6b64456c557c7696ae89a1b7f606ce746d2e33", "bade9f8")
    assert not ok and "前缀" in reason


def test_revision_non_sha_tag_rejects() -> None:
    for tag in ("latest", "v1.0", "agent-t", ""):
        ok, reason = check_revision("ff6b64456c557c7696ae89a1b7f606ce746d2e33", tag)
        assert not ok and "Git SHA" in reason
