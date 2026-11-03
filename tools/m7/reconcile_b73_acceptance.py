# -*- coding: utf-8 -*-
"""B7-3 换装验收 · 账目核对器（零付费工具，不发起任何模型调用）。

对账口径（deploy-memo-b73 §五.4 v5，codex #168 四轮 P1×2 修正）：

- **用量校验收严（四轮 P1-1）**：usage 必须是对象且 output_tokens/
  input_tokens **两个键都存在**、均为非负有限数值——缺失/null/空对象/
  非数值/负数/inf/nan 一律「费用未知」，绝不用 `or 0` 把未知转零元
- **归属必须可证明（四轮 P1-2）**：取行口径只有两条——
  `review_id = 验收单号` 或 `trace_id ∈ executor 记录的三枚 trace`
  （trace 单号每次请求唯一，按 trace 归属可证明）。**水位增量扫描
  只做诊断、绝不计入**：并发时其他用户的账行无法证明归属，宁可
  费用未知=停（executor 若能从服务端日志拿到上传 trace，则按 trace
  归属，可证明）
- **预期清单=独立证据（四轮修正）**：每一笔预期都要有账本之外的依据
  ——suggest=0（force 请求参数）；revision/impact=1（executor 主动
  触发拿到 200）；model_review/quality=审查单数据里的条目/维度数；
  precheck ∈ [1,2]（响应 performed 证明 ≥1；第 2 笔仅服务内部分支、
  外部无独立证据——区间内不外推，第二笔完整性由供应商账单事后复核
  兜底，如实声明）。低于区间下界=缺账（费用未知）；高于上界=多行
  （费用未知）

用法（换装验收执行时）：
    python -X utf8 tools/m7/reconcile_b73_acceptance.py \
        --db <server_db> --review <rid> \
        --trace <upload_trace> --trace <revision_trace> --trace <impact_trace> \
        --expect precheck=1..2 model_review=6 quality=6 revision=1 impact=1
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3

# 峰值估算牌价（估算非实付）：输出 ¥8/M、输入 ¥2/M
PRICE_OUT_CNY_PER_M = 8.0
PRICE_IN_CNY_PER_M = 2.0

_OK = "ok"
_UNKNOWN = "unknown"

_REQUIRED_USAGE_KEYS = ("output_tokens", "input_tokens")


def _parse_usage(raw: object) -> object:
    """账本 usage 列是 JSON 字符串；解析失败返回非对象（后续校验判非法）。"""
    if not isinstance(raw, str):
        return raw
    try:
        return json.loads(raw)
    except ValueError:
        return {"__unparsable__": True}


def _valid_usage(usage: object) -> tuple[bool, float, float]:
    """usage 必须是对象且两个 token 键都为非负有限数值。
    缺一键/null/空对象/字符串/负数/inf/nan → (False, 0, 0)。"""
    if not isinstance(usage, dict):
        return False, 0.0, 0.0
    vals: dict[str, float] = {}
    for key in _REQUIRED_USAGE_KEYS:
        if key not in usage:
            return False, 0.0, 0.0
        v = usage[key]
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return False, 0.0, 0.0
        if not math.isfinite(v) or v < 0:
            return False, 0.0, 0.0
        vals[key] = float(v)
    return True, vals["output_tokens"], vals["input_tokens"]


def _interval_ok(actual: int, want) -> bool:
    """want 为 int（精确）或 [min, max]（区间，如 precheck [1,2]）。"""
    if isinstance(want, list):
        return want[0] <= actual <= want[1]
    return actual == want


def _interval_label(want) -> str:
    if isinstance(want, list):
        return f"{want[0]}..{want[1]}"
    return str(want)


def reconcile(db_path: str, *, review_id: str | None,
              traces: list[str] | None = None,
              watermark: float = 0.0,
              expected: dict[str, object]) -> dict:
    """按 review_id + trace 集合取账行（**可证明归属**），与预期清单
    （独立证据）逐笔比对。水位只出诊断不计入。返回
    {"status": ok|unknown, ...}；unknown 时附 reason 列表；
    任何 SQL 异常 = 查询失败 = unknown（调用方须停）。"""
    reasons: list[str] = []
    rows: list[dict] = []
    traces = [t for t in (traces or []) if t]
    if not review_id and not traces:
        return {"status": _UNKNOWN,
                "reasons": ["无可证明归属的取行口径（未提供 review_id/"
                            "trace）——并发下按时间窗归属不可证明"]}
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        try:
            clauses: list[str] = []
            params: list = []
            if review_id:
                clauses.append("review_id = ?")
                params.append(review_id)
            if traces:
                clauses.append(
                    "trace_id IN (%s)" % ",".join("?" * len(traces)))
                params.extend(traces)
            sql = ("SELECT call_id, review_id, node, provider, model,"
                   " outcome, usage, trace_id, created_at FROM llm_calls"
                   " WHERE (" + " OR ".join(clauses) + ")")
            rows = [dict(r) for r in con.execute(sql, params).fetchall()]
            # 诊断（不计入）：水位后的其他上传节点行——并发下无法证明
            # 归属，若出现说明可能混入他人账行，如实上报
            diag_params: list = [watermark]
            diag_sql = ("SELECT call_id, node, trace_id, review_id"
                        " FROM llm_calls WHERE created_at > ? AND node IN"
                        " ('precheck', 'category_suggest')")
            diag = [dict(r) for r in
                    con.execute(diag_sql, diag_params).fetchall()]
        finally:
            con.close()
    except sqlite3.Error as e:
        return {"status": _UNKNOWN,
                "reasons": [f"账本查询失败：{type(e).__name__}"]}

    # 归属行去重（call_id 主键唯一，OR 口径可能重复取到）
    seen: dict[str, dict] = {}
    for r in rows:
        seen[r["call_id"]] = r

    # 诊断：水位后的上传节点行中，凡不属于已证明归属集合的，列出来
    # 供人工复核（不计入对账）；出现即提示并发混入风险
    unattributed_candidates = [
        d for d in diag if d["call_id"] not in seen
    ]
    if unattributed_candidates:
        reasons.append(
            f"诊断：水位后有 {len(unattributed_candidates)} 条上传节点"
            "账行无法证明归属本次验收（并发混入可能），未计入对账——"
            "请人工核对该批 call_id")

    # 行集合完整性：按 node 聚合实际行数 vs 独立证据预期（精确/区间）
    actual: dict[str, int] = {}
    for r in seen.values():
        actual[r["node"]] = actual.get(r["node"], 0) + 1
    for node, want in sorted(expected.items(), key=lambda kv: kv[0]):
        got = actual.get(node, 0)
        if not _interval_ok(got, want):
            reasons.append(f"{node} 行数不符：预期 {_interval_label(want)}"
                           f" 实际 {got}")
    for node, got in sorted(actual.items()):
        if node not in expected:
            reasons.append(f"预期外节点行：{node}×{got}")

    # 逐行 usage / outcome 完整性（收严口径，四轮 P1-1）
    total_cny = 0.0
    for r in sorted(seen.values(), key=lambda x: str(x["created_at"])):
        if r["outcome"] not in ("success", "parse_failed"):
            reasons.append(f"{r['node']} 行 outcome 异常：{r['outcome']}"
                           f"（call_id={r['call_id']}）")
            continue
        ok_usage, out_tok, in_tok = _valid_usage(_parse_usage(r["usage"]))
        if not ok_usage:
            reasons.append(f"{r['node']} 行 usage 缺失/不完整/非法"
                           f"（call_id={r['call_id']}）——费用未知")
            continue
        total_cny += (out_tok * PRICE_OUT_CNY_PER_M
                      + in_tok * PRICE_IN_CNY_PER_M) / 1_000_000

    status = _OK if not reasons else _UNKNOWN
    return {"status": status, "rows": len(seen), "reasons": reasons,
            "actual_by_node": actual, "expected": expected,
            "total_est_cny": round(total_cny, 4)}


def _parse_expect(kv: str) -> tuple[str, object]:
    """node=3 → 精确 3；node=1..2 → 区间 [1, 2]。"""
    node, _, rng = kv.partition("=")
    if ".." in rng:
        lo, _, hi = rng.partition("..")
        return node, [int(lo), int(hi)]
    return node, int(rng)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--review", default=None)
    ap.add_argument("--trace", action="append", default=[])
    ap.add_argument("--watermark", type=float, default=0.0,
                    help="仅诊断用（并发混入提示），不计入对账")
    ap.add_argument("--expect", nargs="+", required=True,
                    help="node=次数 或 node=下界..上界（如 precheck=1..2）")
    args = ap.parse_args()
    expected = dict(_parse_expect(kv) for kv in args.expect)
    result = reconcile(args.db, review_id=args.review, traces=args.trace,
                       watermark=args.watermark, expected=expected)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == _OK else 2


if __name__ == "__main__":
    raise SystemExit(main())
