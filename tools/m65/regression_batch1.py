"""F-1/F-2 施工批1 · 17 份真实合同规则层回归（合并门禁）。

用法：python -X utf8 tools/m65/regression_batch1.py [--out docs/m65/batch1-regression.json]

退出码即门禁（外审验收整改：回归报告是证据，非零退出码才是门禁）：
- 0：17 份全跑通、基线覆盖完整、靶向四项之外零翻转
- 1：基线覆盖缺文件 / 靶向四项之外出现任何翻转（零回退红线击穿）

口径（金标裁决 v2，docs/m65/f12-golden-ruling.md）：
- F-1：payment/term 漏报逐份定性（未找到剩余数如实点名，真缺失/空白模板
  挂老钱复核，不写死清零目标）
- F-2：breach 对等豁免应翻转，真单方必须保留
- F-4：signature 盖章+落款行应翻转，纯盖章孤行必须保留
- 零回退：payment/term/breach/signature 四项之外的任何 item 状态翻转 = 红线

靶向四项的状态变化属于本批授权范围（人工对照金标裁决逐份定性）；
其余 item 的翻转直接击穿门禁，需在金标裁决追认后显式更新 AUTHORIZED_TARGET。
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

# 17 份官方语料是本批回归的固定盘点（金标裁决材料集）；新增合同必须
# 显式改这个数并同步 run1/run2 基线——防「悄悄少了合同也全绿」
EXPECTED_CONTRACTS = 17
# 本批授权观察的检查项（状态变化人工对照金标裁决）；此外任何翻转都击穿门禁
AUTHORIZED_TARGET = frozenset({"payment", "term", "breach", "signature"})


def _baseline() -> dict[str, dict[str, str]]:
    """run1+run2 合并为修复前基线：file -> {item_id: status}。"""
    base: dict[str, dict[str, str]] = {}
    for run in ("run1", "run2"):
        d = json.loads((ROOT / "docs" / "m65" / f"{run}.json").read_text(encoding="utf-8"))
        for c in d["contracts"]:
            base[c["file"]] = {i["id"]: i["status"] for i in c.get("items_detail") or []}
    return base


def _load_manifest() -> dict[str, str]:
    """语料盘点（测试通过 monkeypatch 本函数模拟盘点漂移）。"""
    return json.loads((ROOT / "fixtures-real" / "manifest.json").read_text(encoding="utf-8"))


def run_regression(out_path: str | None) -> tuple[int, dict]:
    """跑全量回归，返回 (退出码, 报告对象)。"""
    manifest = _load_manifest()
    if len(manifest) != EXPECTED_CONTRACTS:
        print(f"🔴 回归合同数 {len(manifest)} ≠ 固定盘点 {EXPECTED_CONTRACTS}"
              f"——盘点变化必须显式更新 EXPECTED_CONTRACTS 并同步基线")
        return 1, {}
    base = _baseline()
    missing = sorted(set(manifest) - set(base))
    if missing:
        print(f"🔴 基线覆盖缺失（run1/run2 无这些文件的基线）: {missing}")
        return 1, {}

    rows: list[dict] = []
    flipped_non_target = 0
    for fname, category in manifest.items():
        text = _full_text(ROOT / "fixtures-real", fname)
        result = run_checklist(text, category)
        now = {i["id"]: i["status"] for i in result["items"]}
        old = base.get(fname, {})
        diffs = {k: (old.get(k), now[k]) for k in now if old.get(k) != now[k]}
        extra = {k: v for k, v in diffs.items() if k not in AUTHORIZED_TARGET}
        flipped_non_target += len(extra)
        rows.append({
            "file": fname, "category": category,
            "diffs": {k: {"was": v[0], "now": v[1]} for k, v in diffs.items()},
            "extra_flips": extra,
            "live": now,
            "breach_note": next((i.get("note") or "" for i in result["items"]
                                 if i["id"] == "breach"), ""),
        })

    # live 全量汇总（小智娘 P2-1 整改：只数「翻转行」会把未翻转的持续
    # 未找到漏计成假 0；live 状态按当前判定全量取）
    live = {r["file"]: r["live"] for r in rows}
    pay_now = sorted(f for f, m in live.items() if m.get("payment") == "未找到")
    term_now = sorted(f for f, m in live.items() if m.get("term") == "未找到")
    breach_att = sorted(f for f, m in live.items() if m.get("breach") == "需关注")
    sig_att = sorted(f for f, m in live.items() if m.get("signature") == "需关注")
    print("=" * 70)
    print(f"payment 未找到（live 全量，待逐份定性）: {len(pay_now)} {pay_now}")
    print(f"term 未找到（live 全量，待逐份定性）: {len(term_now)} {term_now}")
    print(f"breach 需关注（live 全量）: {len(breach_att)} {breach_att}")
    print(f"signature 需关注（live 全量）: {len(sig_att)} {sig_att}")
    print("=" * 70)
    for r in rows:
        if r["diffs"]:
            print(f"\n◆ {r['file']}  [{r['category']}]")
            for k, v in r["diffs"].items():
                print(f"   {k}: {v['was']} -> {v['now']}")
    print("=" * 70)
    print(f"靶向四项之外翻转数（零回退红线，非 0 即退出码 1）: {flipped_non_target}")

    report = {"rows": rows, "flipped_non_target": flipped_non_target}
    if out_path:
        Path(out_path).write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"明细已写 {out_path}")

    if flipped_non_target:
        print(f"🔴 零回退红线击穿：{flipped_non_target} 条靶向外翻转（见上方 extra_flips）"
              f"——须金标裁决追认后更新 AUTHORIZED_TARGET，或回退改动")
        return 1, report
    return 0, report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "docs" / "m65" / "batch1-regression.json"))
    args = ap.parse_args(argv)
    rc, _ = run_regression(args.out)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
