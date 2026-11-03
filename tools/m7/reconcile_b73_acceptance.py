# -*- coding: utf-8 -*-
"""B7-3 换装验收 · 账目核对器（零付费工具，不发起任何模型调用）。

对账口径（deploy-memo-b73 §五.4 v4，codex #168 三轮 P1×2 修正）：
- **分 trace 记账**：上传（precheck）走上传 trace（reviews.data.trace_id）；
  revision / impact 各自生成新 trace（revision_drafts.trace_id /
  impact_reports.trace_id）——**全链不是同一个 trace**，按
  review_id + trace 集合合并取行
- **无单号也有账可查**：上传未建单（槽满 429/建单失败）时，凭执行前
  水位（max created_at）增量扫描上传阶段节点（precheck/category_suggest）
  ——查不到应收账行 = 费用未知 = 停
- **预期清单=实际执行分支**（executor 如实记录，不是费用表的
  「最多次数」——那是预留口径）；逐笔比对：缺行/多行/缺 usage/
  outcome 非正常/查询失败 = 费用未知
- STRICT_SPENT_CODE 的边界如实限定：它只查已存在行的 usage 完整性，
  不证明应有行存在——行集合完整性由本脚本与预期清单比对补齐

用法（换装验收执行时）：
    python -X utf8 tools/m7/reconcile_b73_acceptance.py \
        --db <server_db> --review <rid> \
        --trace <upload_trace> --trace <revision_trace> --trace <impact_trace> \
        --watermark <float> --expect precheck=1 model_review=6 quality=6 revision=1 impact=1
"""
from __future__ import annotations

import argparse
import json
import sqlite3

# 峰值估算牌价（估算非实付）：输出 ¥8/M、输入 ¥2/M
PRICE_OUT_CNY_PER_M = 8.0
PRICE_IN_CNY_PER_M = 2.0

_OK = "ok"
_UNKNOWN = "unknown"


def reconcile(db_path: str, *, review_id: str | None,
              traces: list[str] | None = None,
              watermark: float = 0.0,
              upload_nodes: tuple[str, ...] = ("precheck",
                                               "category_suggest"),
              expected: dict[str, int]) -> dict:
    """按 review_id + trace 集合 + 水位增量取账行，与预期清单逐笔比对。

    返回 {"status": ok|unknown, ...}；unknown 时附 reason 列表。
    任何 SQL 异常 = 查询失败 = unknown（调用方须停）。"""
    reasons: list[str] = []
    rows: list[dict] = []
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        try:
            traces = [t for t in (traces or []) if t]
            clauses: list[str] = []
            params: list = []
            if review_id:
                clauses.append("review_id = ?")
                params.append(review_id)
            if traces:
                clauses.append(
                    "trace_id IN (%s)" % ",".join("?" * len(traces)))
                params.extend(traces)
            if upload_nodes:
                clauses.append(
                    "(created_at > ? AND node IN (%s))"
                    % ",".join("?" * len(upload_nodes)))
                params.extend([watermark, *upload_nodes])
            if not clauses:
                return {"status": _UNKNOWN,
                        "reasons": ["无可执行取行口径（未提供 review_id/"
                                    "trace/水位）"]}
            sql = ("SELECT call_id, review_id, node, provider, model,"
                   " outcome, usage, trace_id, created_at FROM llm_calls"
                   " WHERE (" + " OR ".join(clauses) + ")")
            rows = [dict(r) for r in con.execute(sql, params).fetchall()]
        finally:
            con.close()
    except sqlite3.Error as e:
        return {"status": _UNKNOWN,
                "reasons": [f"账本查询失败：{type(e).__name__}"]}

    # 去重（call_id 主键天然唯一，OR 口径可能重复取到同一行）
    seen: dict[str, dict] = {}
    for r in rows:
        seen[r["call_id"]] = r

    # 行集合完整性：按 node 聚合实际行数 vs 预期
    actual: dict[str, int] = {}
    for r in seen.values():
        actual[r["node"]] = actual.get(r["node"], 0) + 1
    for node, want in sorted(expected.items()):
        got = actual.get(node, 0)
        if got < want:
            reasons.append(f"{node} 缺行：预期 {want} 实际 {got}")
        elif got > want:
            reasons.append(f"{node} 多行：预期 {want} 实际 {got}")
    for node, got in sorted(actual.items()):
        if node not in expected:
            reasons.append(f"预期外节点行：{node}×{got}")

    # 逐行 usage / outcome 完整性（STRICT 口径的等价实现+explicit 边界）
    total_cny = 0.0
    for r in sorted(seen.values(), key=lambda x: str(x["created_at"])):
        if r["outcome"] not in ("success", "parse_failed"):
            reasons.append(f"{r['node']} 行 outcome 异常：{r['outcome']}"
                           f"（call_id={r['call_id']}）")
            continue
        usage = r["usage"]
        if not usage:
            reasons.append(f"{r['node']} 行缺 usage（call_id="
                           f"{r['call_id']}）——费用未知")
            continue
        try:
            u = json.loads(usage)
            out_tok = float(u.get("output_tokens") or 0)
            in_tok = float(u.get("input_tokens") or 0)
            total_cny += (out_tok * PRICE_OUT_CNY_PER_M
                          + in_tok * PRICE_IN_CNY_PER_M) / 1_000_000
        except (ValueError, TypeError, AttributeError):
            reasons.append(f"{r['node']} 行 usage 不可解析（call_id="
                           f"{r['call_id']}）——费用未知")

    status = _OK if not reasons else _UNKNOWN
    return {"status": status, "rows": len(seen), "reasons": reasons,
            "actual_by_node": actual, "expected": expected,
            "total_est_cny": round(total_cny, 4)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--review", default=None)
    ap.add_argument("--trace", action="append", default=[])
    ap.add_argument("--watermark", type=float, default=0.0)
    ap.add_argument("--expect", nargs="+", required=True,
                    help="node=次数，如 precheck=1 model_review=6")
    args = ap.parse_args()
    expected = {}
    for kv in args.expect:
        node, _, cnt = kv.partition("=")
        expected[node] = int(cnt)
    result = reconcile(args.db, review_id=args.review, traces=args.trace,
                       watermark=args.watermark, expected=expected)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == _OK else 2


if __name__ == "__main__":
    raise SystemExit(main())
