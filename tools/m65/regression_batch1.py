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
其余 item（含 payment/breach/signature）必须逐项等于施工前快照（pre-p2-snapshot.json）。
"""
from __future__ import annotations

import argparse
import hashlib
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
# P2 锚定精确白名单（用户补充⑤）：基准=施工前快照（pre-p2-snapshot.json，
# d575a10）——term 整体放行太宽，写死到「哪份合同的哪个项允许从什么翻成什么」。
# 7 份均经老钱口径验尸实锤为「标签在、值空白」的范本留白（spec-p2 §一改账）：
# school=「自生效之日起＿年」、construction=「工期总日历＿天」、energy=服务期限栏
# 空白、food=「供货服务期＿年」、raw-milk=「履行期限＿年＿月＿日」、
# work-contract=承揽交付期限表格空白、mandate=「委托期限 自＿年＿月＿日至＿年＿月＿日止」。
# （gov 不在列：批1 时已修正为未找到，快照中即是，P2 无翻转——外审二轮勘正）
# 其余 10 份 term 与所有非期限项必须逐项等于快照。
AUTHORIZED_FLIPS = frozenset({
    ("school-uniform-procurement-guangzhou.docx", "term", "通过", "未找到"),
    ("construction-work-contract-2017.docx", "term", "通过", "未找到"),
    ("energy-hosting-service-2026.docx", "term", "通过", "未找到"),
    ("food-procurement-xinjiang-2025.docx", "term", "通过", "未找到"),
    ("raw-milk-purchase-2016.docx", "term", "通过", "未找到"),
    ("work-contract-gf-2000.docx", "term", "通过", "未找到"),
    ("mandate-contract-samr-2025.docx", "term", "通过", "未找到"),
})


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


def _load_snapshot() -> dict:
    """施工前快照（外审 PR #94 修复卡①：来源 d575a10，含合同文件哈希）。"""
    return json.loads((ROOT / "docs" / "m65" / "pre-p2-snapshot.json").read_text(encoding="utf-8"))


def run_regression(out_path: str | None, live_override: dict[str, dict[str, str]] | None = None
                   ) -> tuple[int, dict]:
    """跑全量回归，返回 (退出码, 报告对象)。

    live_override（仅测试注入）：跳过引擎实跑，直接采用给定 live 状态——
    守门员测试用它在 payment/breach/signature 上人为注入变化。"""
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
    # 施工前快照（外审 PR #94 修复卡①③）：门禁逐项基准——payment/breach/
    # signature 与全部其他项必须逐项等于快照，唯一例外=AUTHORIZED_FLIPS 四元组
    snapshot = _load_snapshot()
    if snapshot.get("source_commit") != "d575a10":
        print(f"🔴 快照来源提交 {snapshot.get('source_commit')} ≠ d575a10（快照被换，不可信）")
        return 1, {}

    rows: list[dict] = []
    flipped_non_target = 0
    authorized_seen: set[tuple[str, str, str | None, str | None]] = set()
    for fname, category in manifest.items():
        if live_override is not None:
            now = live_override[fname]
            note = ""
        else:
            text = _full_text(ROOT / "fixtures-real", fname)
            result = run_checklist(text, category)
            now = {i["id"]: i["status"] for i in result["items"]}
            note = next((i.get("note") or "" for i in result["items"]
                         if i["id"] == "breach"), "")
        # 合同文件指纹（防语料漂移——快照针对的原文变了，快照对照即失效）
        cur_sha = hashlib.sha256((ROOT / "fixtures-real" / fname).read_bytes()).hexdigest()
        if cur_sha != snapshot["contracts"][fname]["file_sha256"]:
            print(f"🔴 {fname} 文件指纹与快照不一致——语料被改动，快照对照失效")
            return 1, {}
        old = snapshot["contracts"][fname]["status_map"]
        # 键取快照∪当前的并集（外审 PR #92 销项：防删/改名键漏计）
        diffs = {k: (old.get(k), now.get(k))
                 for k in old.keys() | now.keys() if old.get(k) != now.get(k)}
        extra = {}
        for k, v in diffs.items():
            quad = (fname, k, v[0], v[1])
            # P2 唯一例外：白名单四元组；payment/breach/signature 及全部其他项
            # 一律逐项等于快照（外审 PR #94 修复卡③）
            if quad in AUTHORIZED_FLIPS:
                authorized_seen.add(quad)
            else:
                extra[k] = v
        flipped_non_target += len(extra)
        rows.append({
            "file": fname, "category": category,
            "diffs": {k: {"was": v[0], "now": v[1]} for k, v in diffs.items()},
            "extra_flips": extra,
            "live": now,
            "breach_note": note,
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

    # 授权翻转正向断言（外审 P2 #94 销项③）：白名单翻转必须**确实发生**——
    # 只拦非法翻转的话，未来回归把四份合同改回「通过」会产生零 diff 溜过门禁。
    # live 现状必须等于授权的 now 状态（term=未找到）。
    live = {r["file"]: r["live"] for r in rows}
    authorize_fail = 0
    for fname, item_id, _was, now in sorted(AUTHORIZED_FLIPS):
        actual = live.get(fname, {}).get(item_id)
        if actual != now:
            authorize_fail += 1
            print(f"🔴 授权翻转缺失：{fname} {item_id} 现状 {actual} ≠ 授权终态 {now}"
                  f"（回归回退了锚定口径，门禁拦截）")
        else:
            print(f"✅ 授权翻转在场: {fname[:40]} {item_id} = {now}")
    flipped_non_target += authorize_fail
    # 总量审计（外审修复卡③）：实际授权翻转集合必须与白名单完全相等——
    # 既不许白名单外的翻转（上方已拦），也不许白名单内的翻转缺席（正向断言），
    # 两者合起来=「不多不少恰好这些改账」
    if authorized_seen != set(AUTHORIZED_FLIPS):
        print(f"🔴 授权翻转集合与白名单不相等：实际 {sorted(authorized_seen)}"
              f" ≠ 白名单 {sorted(AUTHORIZED_FLIPS)}")
        flipped_non_target += 1
    report = {"rows": rows, "flipped_non_target": flipped_non_target,
              "authorized_seen": sorted(authorized_seen)}
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
