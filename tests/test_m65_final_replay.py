# -*- coding: utf-8 -*-
"""收官回测名册纪律测试（Codex P1：名册机器取数+集数漂移必红）。"""
from __future__ import annotations

import json
import pathlib
import sys
from unittest import mock

import pytest

from tools.m65.final_replay import (ARCHIVES, CALLS_PER_NODE,
                                    SPENT_REMOTE_CODE,
                                    EXPECTED_ROSTER_SIZE, MAX_COST_CNY,
                                    PRICE_IN_CNY_PER_M, PRICE_OUT_CNY_PER_M,
                                    check_revision, estimate_cost,
                                    load_roster, preflight_cost_check,
                                    read_review_ids, worst_case_cost_cny,
                                    worst_case_output_tokens_per_contract)

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


def test_estimate_cost_uses_flash_peak_price() -> None:
    """费用单 v3 牌价：生产 deepseek-v4-flash（官方中文页 Flash 列峰值）。"""
    assert PRICE_IN_CNY_PER_M == pytest.approx(2.0)
    assert PRICE_OUT_CNY_PER_M == pytest.approx(8.0)
    assert estimate_cost(1_000_000, 0) == pytest.approx(2.0)
    assert estimate_cost(0, 1_000_000) == pytest.approx(8.0)


def test_worst_case_output_matches_ruling() -> None:
    """Codex P1：逐节点公式读 #137 阀门常量，单卷最坏=270k（裁决口径冻结）。"""
    assert sum(CALLS_PER_NODE.values()) == 18  # 与审查预算对账
    assert worst_case_output_tokens_per_contract() == 270_000


def test_worst_case_total_is_46_20() -> None:
    """总额固化断言：输入+输出同一函数来源，合计=¥46.20（费用单口径）。"""
    assert worst_case_cost_cny() == pytest.approx(46.20, abs=0.01)


def test_valve_bump_makes_preflight_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    """模拟调大一个阀门（model_review 13k→26k）→ 预检必须失败。"""
    from app.services import llm_client

    monkeypatch.setitem(llm_client.NODE_MAX_OUTPUT_TOKENS, "model_review", 26_000)
    bumped = worst_case_cost_cny(valves_override={"model_review": 26_000})
    assert bumped > MAX_COST_CNY, "阀门调大后最坏费用必须超出现有硬上限"


def test_max_cost_covers_worst_case() -> None:
    """阀门/调用数变化而费用上限未同步 → 此测试必红（漂移警报）——共用同一函数。"""
    assert MAX_COST_CNY >= worst_case_cost_cny()


def test_preflight_real_path_balance_short_fails() -> None:
    """真实预检路径：余额 ¥42.80 < ¥47 → 拦截并提示充值金额（Codex P1 覆盖）。"""
    with pytest.raises(SystemExit, match="充值"):
        preflight_cost_check(42.80)


def test_preflight_real_path_balance_ok_returns_verdict() -> None:
    verdict = preflight_cost_check(50.0)
    assert verdict == {"worst_cost_cny": 46.21, "hard_cap_cny": 47.0,
                       "balance_cny": 50.0}


def test_preflight_real_path_valve_drift_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """真实预检路径：阀门调大→机算最坏超硬上限→即使余额充足也拦（漂移警报）。"""
    from app.services import llm_client

    monkeypatch.setitem(llm_client.NODE_MAX_OUTPUT_TOKENS, "model_review", 26_000)
    with pytest.raises(SystemExit, match="费用上限须走 PR 同步"):
        preflight_cost_check(50.0)


def test_read_review_ids_tolerates_half_line(tmp_path: pathlib.Path) -> None:
    """JSONL 半行（写入中崩溃）容忍：完整行照读，半行不炸。"""
    f = tmp_path / "ids.jsonl"
    f.write_text(
        '{"file": "a.docx", "review_id": "r1"}\n'
        '{"file": "b.docx", "review_id": "r2"}\n'
        '{"file": "c.docx", "revi', encoding="utf-8")  # 半行
    assert read_review_ids(f) == ["r1", "r2"]


def test_read_review_ids_missing_file_returns_empty(tmp_path: pathlib.Path) -> None:
    assert read_review_ids(tmp_path / "not-exist.jsonl") == []


def test_spent_for_reviews_sql_filters_by_review_id(tmp_path: pathlib.Path) -> None:
    """Codex 勘正验收：SQL 按 review_id IN 精确过滤——隔壁桌的钱不算进来。"""
    import sqlite3
    db = tmp_path / "led.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE llm_calls (review_id TEXT, usage TEXT)")
    con.execute("INSERT INTO llm_calls VALUES (?, ?)",
                ("r_batch", json.dumps({"input_tokens": 1000, "output_tokens": 500})))
    con.execute("INSERT INTO llm_calls VALUES (?, ?)",
                ("r_other", json.dumps({"input_tokens": 9000, "output_tokens": 9000})))
    con.commit()
    con.close()
    import subprocess
    r = subprocess.run(
        [sys.executable,
         "-c", SPENT_REMOTE_CODE, str(db), "r_batch"],
        capture_output=True, text=True, check=True)
    i, o = json.loads(r.stdout.strip().splitlines()[-1])
    assert (i, o) == (1000, 500)  # 隔壁桌 r_other 的 9000/9000 一分没算
    # 多 id 场景：逗号串联逐一参数化
    r = subprocess.run(
        [sys.executable, "-c", SPENT_REMOTE_CODE, str(db), "r_batch,r_other"],
        capture_output=True, text=True, check=True)
    i2, o2 = json.loads(r.stdout.strip().splitlines()[-1])
    assert (i2, o2) == (10000, 9500)


def test_review_ids_landed_before_poll(tmp_path: pathlib.Path,
                                       monkeypatch: pytest.MonkeyPatch) -> None:
    """每卷 review_id 在轮询之前就落入 JSONL（哨兵在跑批中就能看到它）。"""
    import tools.m65.run_eval as re_eval

    ids_file = tmp_path / "ids.jsonl"
    captured: dict = {}

    def fake_poll(client, base, rid):
        captured["at_poll"] = (ids_file.read_text(encoding="utf-8")
                               if ids_file.exists() else "")
        raise RuntimeError("short-circuit")  # 拿到轮询证物后即短路后续重活

    monkeypatch.setattr(re_eval, "upload_contract", lambda *a, **k: "ridX")
    monkeypatch.setattr(re_eval, "poll_review", fake_poll)
    paper = tmp_path / "c.txt"
    paper.write_text("x", encoding="utf-8")
    re_eval.eval_contract(mock.Mock(), "http://base", paper, "procurement",
                          review_ids_out=str(ids_file))
    assert "ridX" in captured["at_poll"]
