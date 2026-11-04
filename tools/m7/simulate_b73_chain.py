# -*- coding: utf-8 -*-
"""B7-3 换装验收 · 模拟完整流水演示（零付费，纯本地 SQLite）。

按 codex #168 五轮要求，用模拟流水回答三件事：
1. **现状实锤**：单账（只有账本）模式下，precheck 实际 2 笔、第二笔
   账行丢失 + 区间预期 [1,2] → 核对器返回 ok——**逐笔漏账无法当场
   发现**（transport 单点只有账本一本账，已核代码无账本之外的逐笔
   记录——这就是局限本身）
2. **双账模式机制验证**：引入第二本账（transport 审计日志行，与账本
   emit 同点写）后，同一场景（删掉第二次 precheck 账行）→ unknown
3. **质量层合并调用反例**：短合同多维度合并 1 次调用 ≠ 维度数——
   按「维度数当次数」会误报；按审计行数当次数不误报

结论呈两案（deploy-memo-b73 §五.4 v6）：方案一=transport 审计日志一行
（生产代码 5 行级 fail-safe 变更，呈批）；方案二=接受局限声明。

用法：python -X utf8 tools/m7/simulate_b73_chain.py
"""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from tools.m7.reconcile_b73_acceptance import reconcile  # noqa: E402

_W = 1000.0  # 水位
R_ID = "r-sim"


def _seed(db: str, rows: list[dict]) -> None:
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE llm_calls ("
        "call_id TEXT PRIMARY KEY, review_id TEXT, node TEXT NOT NULL,"
        "provider TEXT NOT NULL, model TEXT NOT NULL, outcome TEXT NOT NULL,"
        "usage TEXT, trace_id TEXT, created_at REAL NOT NULL)")
    for r in rows:
        base = {"provider": "deepseek", "model": "flash",
                "outcome": "success"}
        base.update(r)
        cols = sorted(base.keys())
        con.execute(
            f"INSERT INTO llm_calls ({','.join(cols)})"
            f" VALUES ({','.join('?' * len(cols))})",
            [base[c] for c in cols])
    con.commit()
    con.close()


def _usage() -> str:
    return json.dumps({"output_tokens": 1000, "input_tokens": 500})


def _full_rows(precheck_rows: int = 2) -> list[dict]:
    """模拟完整流水：上传(precheck×2 含解析失败重试)→评审→质量层
    （短合同合并 1 次）→改写→牵连。"""
    rows = [
        {"call_id": "s-pre1", "review_id": None, "node": "precheck",
         "trace_id": "up-t", "usage": _usage(), "created_at": _W + 1},
        {"call_id": "s-pre2", "review_id": None, "node": "precheck",
         "trace_id": "up-t", "usage": _usage(), "created_at": _W + 2},
        {"call_id": "s-mr", "review_id": R_ID, "node": "model_review",
         "trace_id": "up-t", "usage": _usage(), "created_at": _W + 3},
        {"call_id": "s-q", "review_id": R_ID, "node": "quality",
         "trace_id": "up-t", "usage": _usage(), "created_at": _W + 4},
        {"call_id": "s-rev", "review_id": R_ID, "node": "revision",
         "trace_id": "rev-t", "usage": _usage(), "created_at": _W + 5},
        {"call_id": "s-imp", "review_id": R_ID, "node": "impact",
         "trace_id": "imp-t", "usage": _usage(), "created_at": _W + 6},
    ]
    return [r for r in rows
            if r["call_id"] != "s-pre2" or precheck_rows >= 2]


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="b73-sim-")

    # ---- 场景 1：现状单账 + 区间预期，删第二次 precheck 行 → ok（局限实锤）----
    db1 = str(pathlib.Path(tmp) / "s1.db")
    _seed(db1, _full_rows(precheck_rows=1))  # 人为删掉第二次 precheck 账行
    r1 = reconcile(db1, review_id=R_ID, traces=["up-t", "rev-t", "imp-t"],
                   watermark=_W,
                   expected={"precheck": [1, 2], "model_review": 1,
                             "quality": 1, "revision": 1, "impact": 1})
    print("场景1（现状单账+区间）：删第二笔 precheck 行 →", r1["status"])
    print("  →", r1["reasons"] or "无异常提示——逐笔漏账无法当场发现")
    assert r1["status"] == "ok", "现状局限应如实复现为 ok"

    # ---- 场景 2：双账模式，同一删行 → unknown ----
    db2 = str(pathlib.Path(tmp) / "s2.db")
    _seed(db2, _full_rows(precheck_rows=1))
    audit = [
        {"call_id": "s-pre1", "node": "precheck"},
        {"call_id": "s-pre2", "node": "precheck"},  # 审计行在、账行被删
        {"call_id": "s-mr", "node": "model_review"},
        {"call_id": "s-q", "node": "quality"},
        {"call_id": "s-rev", "node": "revision"},
        {"call_id": "s-imp", "node": "impact"},
    ]
    r2 = reconcile(db2, review_id=R_ID, traces=["up-t", "rev-t", "imp-t"],
                   watermark=_W, expected={}, audit_lines=audit)
    print("场景2（双账模式）：同一删行 →", r2["status"])
    print("  →", [x for x in r2["reasons"] if "缺行" in x])
    assert r2["status"] == "unknown", "双账模式必须当场发现缺行"

    # ---- 场景 3：双账模式完整链 → ok；质量层合并调用不误报 ----
    db3 = str(pathlib.Path(tmp) / "s3.db")
    _seed(db3, _full_rows(precheck_rows=2))
    r3 = reconcile(db3, review_id=R_ID, traces=["up-t", "rev-t", "imp-t"],
                   watermark=_W, expected={}, audit_lines=audit)
    print("场景3（双账模式）：完整链 →", r3["status"],
          "| 实际行数", r3["rows"])
    # 质量层实际 1 次合并调用（≠维度数）：按审计行数当次数 → 不误报
    assert r3["status"] == "ok" and r3["actual_by_node"]["quality"] == 1

    print("\n结论：现状单账无法当场发现逐笔漏账（局限实锤）；双账模式"
          "（transport 审计日志+账本同点写）可当场发现。两案呈批见 "
          "deploy-memo-b73 §五.4 v6。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
