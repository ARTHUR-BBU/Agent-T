"""F-1/F-2 施工批1 · 17 份真实合同规则层回归。

用法：python -X utf8 tools/m65/regression_batch1.py [--out docs/m65/batch1-regression.json]

口径（金标裁决 v2，docs/m65/f12-golden-ruling.md）：
- F-1：payment 未找到 9→0；term 未找到 6→仅剩真缺失（水泥 GF-2008 倾向保留）
- F-2：breach 需关注 4 条嫌疑逐条复核——对等豁免应翻转，真单方必须保留
- F-4：signature 仅盖章需关注——有盖章+落款行的应翻转，纯盖章孤行必须保留
- 零回退：其余任何 item 状态翻转都要点名（人工对照金标裁决）

判定只报差异不判死刑：本脚本输出对照表供验收引用，最终解释权在金标裁决。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.services.checklist import run_checklist  # noqa: E402
from tools.m65.analyze_f12 import _full_text  # noqa: E402


def _baseline() -> dict[str, dict[str, str]]:
    """run1+run2 合并为修复前基线：file -> {item_id: status}。"""
    base: dict[str, dict[str, str]] = {}
    for run in ("run1", "run2"):
        d = json.loads((ROOT / "docs" / "m65" / f"{run}.json").read_text(encoding="utf-8"))
        for c in d["contracts"]:
            base[c["file"]] = {i["id"]: i["status"] for i in c.get("items_detail") or []}
    return base


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "docs" / "m65" / "batch1-regression.json"))
    args = ap.parse_args()

    manifest = json.loads((ROOT / "fixtures-real" / "manifest.json").read_text(encoding="utf-8"))
    base = _baseline()
    rows: list[dict] = []
    flipped_non_target = 0

    for fname, category in manifest.items():
        text = _full_text(ROOT / "fixtures-real", fname)
        result = run_checklist(text, category)
        now = {i["id"]: i["status"] for i in result["items"]}
        old = base.get(fname, {})
        diffs = {k: (old.get(k), now[k]) for k in now if old.get(k) != now[k]}
        # 靶向四项之外的翻转 = 零回退红线，逐条点名
        target = {"payment", "term", "breach", "signature"}
        extra = {k: v for k, v in diffs.items() if k not in target}
        flipped_non_target += len(extra)
        rows.append({
            "file": fname, "category": category,
            "diffs": {k: {"was": v[0], "now": v[1]} for k, v in diffs.items()},
            "extra_flips": extra,
            "breach_note": next((i.get("note") or "" for i in result["items"]
                                 if i["id"] == "breach"), ""),
        })

    # 指标汇总：靶向四项按「基线状态 + diff」还原修复后全景
    def _status_map(r: dict) -> dict[str, tuple[str | None, str]]:
        return {k: (v["was"], v["now"]) for k, v in r["diffs"].items()}

    pay_now = sum(1 for r in rows
                  for k, (was, now) in _status_map(r).items()
                  if k == "payment" and now == "未找到")
    term_now = [(r["file"], was, now) for r in rows
                for k, (was, now) in _status_map(r).items()
                if k == "term" and now == "未找到"]
    print("=" * 70)
    for r in rows:
        if r["diffs"]:
            print(f"\n◆ {r['file']}  [{r['category']}]")
            for k, v in r["diffs"].items():
                print(f"   {k}: {v['was']} -> {v['now']}")
    print("=" * 70)
    print(f"payment 未找到（修复后，金标要求 0）: {pay_now}")
    print(f"term 未找到（修复后，仅允许真缺失）: {term_now or '无'}")
    print(f"靶向四项之外翻转数（零回退红线，应为 0）: {flipped_non_target}")

    if args.out:
        Path(args.out).write_text(
            json.dumps({"rows": rows, "flipped_non_target": flipped_non_target},
                       ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"明细已写 {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
