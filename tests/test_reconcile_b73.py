# -*- coding: utf-8 -*-
"""B7-3 换装验收核对器测试（deploy-memo-b73 §五.4 v4；零付费——纯本地
SQLite 种行，不发任何模型调用）。

场景覆盖（codex #168 三轮 P1×2 要求的正反验证）：
- 完整链（review_id+3 trace 分账合并）→ ok
- 分 trace：revision/impact 各自 trace 不在上传 trace 里也能取到
- 缺行 → unknown；多行/预期外节点 → unknown；缺 usage → unknown
- 无单号（429/建单失败）：凭水位增量扫到上传阶段 precheck 行 → ok；
  水位前的旧行不误入 → ok；零行应收 → unknown
- usage 不可解析 → unknown；查询失败（坏库）→ unknown
"""
from __future__ import annotations

import json
import sqlite3

from tools.m7.reconcile_b73_acceptance import reconcile


def _seed(db_path: str, rows: list[dict]) -> None:
    con = sqlite3.connect(db_path)
    con.execute(
        "CREATE TABLE IF NOT EXISTS llm_calls ("
        "call_id TEXT PRIMARY KEY, review_id TEXT, node TEXT NOT NULL,"
        "provider TEXT NOT NULL, model TEXT NOT NULL, prompt_version TEXT,"
        "document_version TEXT, chars_sent INTEGER, truncated INTEGER,"
        "attempt INTEGER DEFAULT 1, started_at TEXT, latency_ms INTEGER,"
        "outcome TEXT NOT NULL, usage TEXT, gate_counts TEXT,"
        "error_detail TEXT, trace_id TEXT, raw_output TEXT,"
        "prompt_hash TEXT, created_at REAL NOT NULL)")
    for r in rows:
        base = {"provider": "deepseek", "model": "flash",
                "outcome": "success", "attempt": 1}
        base.update(r)
        cols = sorted(base.keys())
        con.execute(
            f"INSERT INTO llm_calls ({','.join(cols)})"
            f" VALUES ({','.join('?' * len(cols))})",
            [base[c] for c in cols])
    con.commit()
    con.close()


def _usage(out=1000, inp=500) -> str:
    return json.dumps({"output_tokens": out, "input_tokens": inp})


T_UP, T_REV, T_IMP = "up-trace", "rev-trace", "imp-trace"


def _full_chain_rows(rid="r-acc", wm=100.0) -> list[dict]:
    return [
        {"call_id": "c-pre", "review_id": None, "node": "precheck",
         "trace_id": T_UP, "usage": _usage(), "created_at": wm + 1},
        {"call_id": "c-mr", "review_id": rid, "node": "model_review",
         "trace_id": T_UP, "usage": _usage(), "created_at": wm + 2},
        {"call_id": "c-q", "review_id": rid, "node": "quality",
         "trace_id": T_UP, "usage": _usage(), "created_at": wm + 3},
        {"call_id": "c-rev", "review_id": rid, "node": "revision",
         "trace_id": T_REV, "usage": _usage(), "created_at": wm + 4},
        {"call_id": "c-imp", "review_id": rid, "node": "impact",
         "trace_id": T_IMP, "usage": _usage(), "created_at": wm + 5},
    ]


_EXPECT = {"precheck": 1, "model_review": 1, "quality": 1,
           "revision": 1, "impact": 1}


def test_full_chain_ok_with_split_traces(tmp_path):
    """完整链：分 trace（上传/revision/impact 各自号）合并取行 → ok。"""
    db = str(tmp_path / "t.db")
    _seed(db, _full_chain_rows())
    r = reconcile(db, review_id="r-acc",
                  traces=[T_UP, T_REV, T_IMP], watermark=100.0,
                  expected=_EXPECT)
    assert r["status"] == "ok", r
    assert r["rows"] == 5
    assert r["total_est_cny"] > 0


def test_review_id_only_also_ok(tmp_path):
    """建单成功后即使漏记 trace，凭 review_id 也能取全（双保险口径）。"""
    db = str(tmp_path / "t.db")
    _seed(db, _full_chain_rows())
    r = reconcile(db, review_id="r-acc", traces=[], watermark=0.0,
                  expected=_EXPECT)
    assert r["status"] == "ok", r


def test_missing_row_unknown(tmp_path):
    db = str(tmp_path / "t.db")
    rows = [r for r in _full_chain_rows() if r["call_id"] != "c-imp"]
    _seed(db, rows)
    r = reconcile(db, review_id="r-acc",
                  traces=[T_UP, T_REV, T_IMP], watermark=100.0,
                  expected=_EXPECT)
    assert r["status"] == "unknown"
    assert any("impact 缺行" in x for x in r["reasons"])


def test_extra_row_unknown(tmp_path):
    db = str(tmp_path / "t.db")
    rows = _full_chain_rows() + [
        {"call_id": "c-x", "review_id": "r-acc", "node": "ask",
         "trace_id": T_UP, "usage": _usage(), "created_at": 106.0}]
    _seed(db, rows)
    r = reconcile(db, review_id="r-acc",
                  traces=[T_UP, T_REV, T_IMP], watermark=100.0,
                  expected=_EXPECT)
    assert r["status"] == "unknown"
    assert any("预期外节点" in x for x in r["reasons"])


def test_missing_usage_unknown(tmp_path):
    db = str(tmp_path / "t.db")
    rows = _full_chain_rows()
    rows[1] = {**rows[1], "usage": None}
    _seed(db, rows)
    r = reconcile(db, review_id="r-acc",
                  traces=[T_UP, T_REV, T_IMP], watermark=100.0,
                  expected=_EXPECT)
    assert r["status"] == "unknown"
    assert any("缺 usage" in x for x in r["reasons"])


def test_no_review_created_trace_and_watermark_rescue(tmp_path):
    """无单号（429/建单失败）：凭 trace+水位增量扫到上传阶段 precheck 行。"""
    db = str(tmp_path / "t.db")
    _seed(db, [{"call_id": "c-pre", "review_id": None, "node": "precheck",
                "trace_id": T_UP, "usage": _usage(),
                "created_at": 100.0 + 1}])
    r = reconcile(db, review_id=None, traces=[T_UP], watermark=100.0,
                  expected={"precheck": 1})
    assert r["status"] == "ok", r


def test_watermark_excludes_stale_rows(tmp_path):
    """水位前的旧行（上次操作残留）不误入本次对账。"""
    db = str(tmp_path / "t.db")
    _seed(db, [
        {"call_id": "c-old", "review_id": None, "node": "precheck",
         "trace_id": "old-trace", "usage": _usage(), "created_at": 50.0},
        {"call_id": "c-new", "review_id": None, "node": "precheck",
         "trace_id": T_UP, "usage": _usage(), "created_at": 101.0},
    ])
    r = reconcile(db, review_id=None, traces=[T_UP], watermark=100.0,
                  expected={"precheck": 1})
    assert r["status"] == "ok", r
    assert r["rows"] == 1


def test_zero_rows_when_expected_unknown(tmp_path):
    """应收账行一笔都查不到 = 费用未知（宁可停，不当作零元继续）。"""
    db = str(tmp_path / "t.db")
    _seed(db, [])
    r = reconcile(db, review_id=None, traces=[T_UP], watermark=100.0,
                  expected={"precheck": 1})
    assert r["status"] == "unknown"
    assert any("precheck 缺行" in x for x in r["reasons"])


def test_unparsable_usage_unknown(tmp_path):
    db = str(tmp_path / "t.db")
    rows = _full_chain_rows()
    rows[2] = {**rows[2], "usage": "{bad json"}
    _seed(db, rows)
    r = reconcile(db, review_id="r-acc",
                  traces=[T_UP, T_REV, T_IMP], watermark=100.0,
                  expected=_EXPECT)
    assert r["status"] == "unknown"
    assert any("不可解析" in x for x in r["reasons"])


def test_abnormal_outcome_unknown(tmp_path):
    db = str(tmp_path / "t.db")
    rows = _full_chain_rows()
    rows[3] = {**rows[3], "outcome": "budget_exceeded", "usage": None}
    _seed(db, rows)
    r = reconcile(db, review_id="r-acc",
                  traces=[T_UP, T_REV, T_IMP], watermark=100.0,
                  expected=_EXPECT)
    assert r["status"] == "unknown"
    assert any("outcome 异常" in x for x in r["reasons"])


def test_query_failure_unknown(tmp_path):
    r = reconcile(str(tmp_path / "nonexistent.db"), review_id="r",
                  traces=[T_UP], watermark=100.0, expected={"precheck": 1})
    assert r["status"] == "unknown"
    assert any("查询失败" in x for x in r["reasons"])


def test_no_criteria_unknown(tmp_path):
    r = reconcile(":memory:", review_id=None, traces=[], watermark=0.0,
                  expected={"precheck": 1})
    assert r["status"] == "unknown"
