# -*- coding: utf-8 -*-
"""B7-3 换装验收核对器测试（deploy-memo-b73 §五.4 v5；零付费——纯本地
SQLite 种行，不发任何模型调用）。

场景覆盖（codex #168 四轮 P1×2 反例 + 独立证据口径）：
- 用量收严：{} / null / 缺单键 / 负数 / 字符串 / inf → 全部 unknown
  （绝不用 or 0 把未知转零元）
- 归属可证明：只按 review_id+trace 取行；水位后的他人账行只出诊断
  不计入（并发混入反例 → unknown/诊断提示）
- 预期清单区间：precheck=1..2 区间内 ok、低于下界 unknown、高于上界
  unknown
- 完整链分 trace / 缺行 / 多行 / 预期外节点 / 异常 outcome / 坏库 /
  无口径 → unknown
"""
from __future__ import annotations

import json
import sqlite3

from tools.m7.reconcile_b73_acceptance import reconcile

T_UP, T_REV, T_IMP = "up-trace", "rev-trace", "imp-trace"


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


# ---------- 用量收严（四轮 P1-1 反例全录） ----------

def test_usage_empty_object_unknown(tmp_path):
    db = str(tmp_path / "t.db")
    rows = _full_chain_rows()
    rows[1] = {**rows[1], "usage": "{}"}
    _seed(db, rows)
    r = reconcile(db, review_id="r-acc", traces=[T_UP, T_REV, T_IMP],
                  watermark=100.0, expected=_EXPECT)
    assert r["status"] == "unknown"
    # 非法行不计费：总额 = 其余 4 行合法用量（4×0.009=0.036），不是 5 行
    assert abs(r["total_est_cny"] - 0.036) < 1e-9, "未知用量绝不能计入费用"


def test_usage_null_unknown(tmp_path):
    db = str(tmp_path / "t.db")
    rows = _full_chain_rows()
    rows[1] = {**rows[1], "usage": None}
    _seed(db, rows)
    r = reconcile(db, review_id="r-acc", traces=[T_UP, T_REV, T_IMP],
                  watermark=100.0, expected=_EXPECT)
    assert r["status"] == "unknown"


def test_usage_single_field_missing_unknown(tmp_path):
    db = str(tmp_path / "t.db")
    rows = _full_chain_rows()
    rows[1] = {**rows[1], "usage": json.dumps({"output_tokens": 100})}
    _seed(db, rows)
    r = reconcile(db, review_id="r-acc", traces=[T_UP, T_REV, T_IMP],
                  watermark=100.0, expected=_EXPECT)
    assert r["status"] == "unknown"


def test_usage_null_field_value_unknown(tmp_path):
    db = str(tmp_path / "t.db")
    rows = _full_chain_rows()
    rows[1] = {**rows[1], "usage": json.dumps(
        {"output_tokens": 100, "input_tokens": None})}
    _seed(db, rows)
    r = reconcile(db, review_id="r-acc", traces=[T_UP, T_REV, T_IMP],
                  watermark=100.0, expected=_EXPECT)
    assert r["status"] == "unknown"


def test_usage_negative_unknown(tmp_path):
    db = str(tmp_path / "t.db")
    rows = _full_chain_rows()
    rows[1] = {**rows[1], "usage": json.dumps(
        {"output_tokens": -5, "input_tokens": 100})}
    _seed(db, rows)
    r = reconcile(db, review_id="r-acc", traces=[T_UP, T_REV, T_IMP],
                  watermark=100.0, expected=_EXPECT)
    assert r["status"] == "unknown"


def test_usage_string_value_unknown(tmp_path):
    db = str(tmp_path / "t.db")
    rows = _full_chain_rows()
    rows[1] = {**rows[1], "usage": json.dumps(
        {"output_tokens": "1000", "input_tokens": 100})}
    _seed(db, rows)
    r = reconcile(db, review_id="r-acc", traces=[T_UP, T_REV, T_IMP],
                  watermark=100.0, expected=_EXPECT)
    assert r["status"] == "unknown"


def test_usage_inf_and_nan_unknown(tmp_path):
    db = str(tmp_path / "t.db")
    rows = _full_chain_rows()
    rows[1] = {**rows[1], "usage": '{"output_tokens": 1e999,'
               ' "input_tokens": 100}'}
    _seed(db, rows)
    r = reconcile(db, review_id="r-acc", traces=[T_UP, T_REV, T_IMP],
                  watermark=100.0, expected=_EXPECT)
    assert r["status"] == "unknown"


def test_valid_usage_ok_and_costed(tmp_path):
    db = str(tmp_path / "t.db")
    _seed(db, _full_chain_rows())
    r = reconcile(db, review_id="r-acc", traces=[T_UP, T_REV, T_IMP],
                  watermark=100.0, expected=_EXPECT)
    assert r["status"] == "ok", r
    # 5 行 × (1000 输出 + 500 输入) → 5×(1000×8+500×2)/1M = 0.045
    assert abs(r["total_est_cny"] - 0.045) < 1e-9


# ---------- 归属可证明（四轮 P1-2 反例） ----------

def test_concurrent_user_row_not_counted(tmp_path):
    """并发反例：本次上传账行缺失 + 水位后他人 precheck 行——
    他人行只出诊断不计入，必须 unknown（绝不让别人的账补上我的缺账）。"""
    db = str(tmp_path / "t.db")
    rows = [r for r in _full_chain_rows() if r["call_id"] != "c-pre"]
    rows.append({"call_id": "c-other", "review_id": "someone-else",
                 "node": "precheck", "trace_id": "other-trace",
                 "usage": _usage(), "created_at": 100.0 + 2})
    _seed(db, rows)
    r = reconcile(db, review_id="r-acc", traces=[T_UP, T_REV, T_IMP],
                  watermark=100.0, expected=_EXPECT)
    assert r["status"] == "unknown"
    assert any("precheck 行数不符" in x for x in r["reasons"])
    assert any("无法证明归属" in x for x in r["reasons"]), "诊断必须提示混入"


def test_no_review_but_trace_from_server_log_ok(tmp_path):
    """无单号但 executor 从服务端日志拿到上传 trace：按 trace 归属可证明。"""
    db = str(tmp_path / "t.db")
    _seed(db, [{"call_id": "c-pre", "review_id": None, "node": "precheck",
                "trace_id": T_UP, "usage": _usage(), "created_at": 101.0}])
    r = reconcile(db, review_id=None, traces=[T_UP], watermark=100.0,
                  expected={"precheck": 1})
    assert r["status"] == "ok", r


def test_no_review_no_trace_unknown(tmp_path):
    """无单号且拿不到上传 trace：无法证明归属 → unknown（停）。"""
    db = str(tmp_path / "t.db")
    _seed(db, [{"call_id": "c-pre", "review_id": None, "node": "precheck",
                "trace_id": T_UP, "usage": _usage(), "created_at": 101.0}])
    r = reconcile(db, review_id=None, traces=[], watermark=100.0,
                  expected={"precheck": 1})
    assert r["status"] == "unknown"
    assert any("归属" in x for x in r["reasons"])


def test_full_chain_ok_with_split_traces(tmp_path):
    db = str(tmp_path / "t.db")
    _seed(db, _full_chain_rows())
    r = reconcile(db, review_id="r-acc", traces=[T_UP, T_REV, T_IMP],
                  watermark=100.0, expected=_EXPECT)
    assert r["status"] == "ok", r
    assert r["rows"] == 5


def test_missing_row_unknown(tmp_path):
    db = str(tmp_path / "t.db")
    rows = [r for r in _full_chain_rows() if r["call_id"] != "c-imp"]
    _seed(db, rows)
    r = reconcile(db, review_id="r-acc", traces=[T_UP, T_REV, T_IMP],
                  watermark=100.0, expected=_EXPECT)
    assert r["status"] == "unknown"
    assert any("impact 行数不符" in x for x in r["reasons"])


def test_extra_row_unknown(tmp_path):
    db = str(tmp_path / "t.db")
    rows = _full_chain_rows() + [
        {"call_id": "c-x", "review_id": "r-acc", "node": "ask",
         "trace_id": T_UP, "usage": _usage(), "created_at": 106.0}]
    _seed(db, rows)
    r = reconcile(db, review_id="r-acc", traces=[T_UP, T_REV, T_IMP],
                  watermark=100.0, expected=_EXPECT)
    assert r["status"] == "unknown"
    assert any("预期外节点" in x for x in r["reasons"])


# ---------- 预期区间（独立证据口径） ----------

def test_interval_precheck_within_ok(tmp_path):
    db = str(tmp_path / "t.db")
    _seed(db, _full_chain_rows())
    r = reconcile(db, review_id="r-acc", traces=[T_UP, T_REV, T_IMP],
                  watermark=100.0,
                  expected={**_EXPECT, "precheck": [1, 2]})
    assert r["status"] == "ok", r


def test_interval_precheck_zero_unknown(tmp_path):
    """响应证明 performed → 至少 1 笔；账本 0 行 = 缺账 unknown。"""
    db = str(tmp_path / "t.db")
    rows = [r for r in _full_chain_rows() if r["call_id"] != "c-pre"]
    _seed(db, rows)
    r = reconcile(db, review_id="r-acc", traces=[T_UP, T_REV, T_IMP],
                  watermark=100.0,
                  expected={**_EXPECT, "precheck": [1, 2]})
    assert r["status"] == "unknown"


def test_interval_precheck_above_upper_unknown(tmp_path):
    db = str(tmp_path / "t.db")
    rows = _full_chain_rows() + [
        {"call_id": "c-pre3", "review_id": None, "node": "precheck",
         "trace_id": T_UP, "usage": _usage(), "created_at": 107.0},
        {"call_id": "c-pre4", "review_id": None, "node": "precheck",
         "trace_id": T_UP, "usage": _usage(), "created_at": 108.0}]
    _seed(db, rows)
    r = reconcile(db, review_id="r-acc", traces=[T_UP, T_REV, T_IMP],
                  watermark=100.0,
                  expected={**_EXPECT, "precheck": [1, 2]})
    assert r["status"] == "unknown"
    assert any("precheck 行数不符" in x for x in r["reasons"])


# ---------- 既有硬防线 ----------

def test_abnormal_outcome_unknown(tmp_path):
    db = str(tmp_path / "t.db")
    rows = _full_chain_rows()
    rows[3] = {**rows[3], "outcome": "budget_exceeded", "usage": None}
    _seed(db, rows)
    r = reconcile(db, review_id="r-acc", traces=[T_UP, T_REV, T_IMP],
                  watermark=100.0, expected=_EXPECT)
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
