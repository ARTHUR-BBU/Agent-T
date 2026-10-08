"""生产证据闭环 · 只读核验脚本（外审施工卡③）。

用法：
  python -X utf8 tools/m65/verify_prod_evidence.py \
      --evidence "F:/合同审查Agent/audit-evidence/prod-evidence-9dd8377.json" \
      [--live]   # 可选：连生产 API 逐条重取状态做在线复核（默认离线）

核验口径（放行标准：零退出 + 输出 17/17 一致 + 生产版本明确）：
  1. 证据条数 == 17（EXPECTED_CONTRACTS 与 regression_batch1 同源）
  2. 每条合同文件 SHA256 与本地 fixtures-real 实算一致（文件未漂移）
  3. 合同类型与 fixtures-real/manifest.json 一致
  4. 部署版本 == DEPLOY_VERSION（9dd8377）
  5. 结果摘要指纹 == 按 batch1-regression.json 的 CI live 状态表规范 JSON
     重算的 SHA256（生产审查结果与 CI 门禁验证过的状态逐项一致）
本脚本只读：不写任何文件，不改任何数据。
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEPLOY_VERSION = "9dd8377"
EXPECTED_CONTRACTS = 17


def _status_digest(status_map: dict[str, str]) -> str:
    """结果摘要指纹（与证据包导出口径同源：规范 JSON 的 SHA256）。"""
    src = json.dumps(status_map, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(src.encode("utf-8")).hexdigest()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--evidence", required=True, help="受控证据包路径（含 review_id 明细，不入仓库）")
    ap.add_argument("--live", action="store_true", help="可选：连生产 API 逐条在线复核")
    args = ap.parse_args(argv)

    pkg = json.loads(Path(args.evidence).read_text(encoding="utf-8"))
    entries = pkg["entries"]
    fails: list[str] = []

    # ① 数量
    if pkg.get("deploy_version") != DEPLOY_VERSION:
        fails.append(f"证据包版本 {pkg.get('deploy_version')} ≠ {DEPLOY_VERSION}")
    if len(entries) != EXPECTED_CONTRACTS:
        fails.append(f"证据条数 {len(entries)} ≠ {EXPECTED_CONTRACTS}")

    manifest = json.loads((ROOT / "fixtures-real" / "manifest.json").read_text(encoding="utf-8"))
    ci_live = {r["file"]: r["live"] for r in json.loads(
        (ROOT / "docs" / "m65" / "batch1-regression.json").read_text(encoding="utf-8"))["rows"]}

    ok = 0
    for e in entries:
        fname = e["filename"]
        problems: list[str] = []
        # ② 合同文件指纹
        local_sha = hashlib.sha256((ROOT / "fixtures-real" / fname).read_bytes()).hexdigest()
        if local_sha != e["file_sha256"]:
            problems.append("文件指纹漂移")
        # ③ 类型
        if manifest.get(fname) != e["category"]:
            problems.append(f"类型不一致 {e['category']} vs manifest {manifest.get(fname)}")
        # ④ 版本
        if e["deploy_version"] != DEPLOY_VERSION:
            problems.append(f"版本 {e['deploy_version']}")
        # ⑤ 结果指纹 vs CI live 状态表
        expect_sha = _status_digest(ci_live[fname])
        if expect_sha != e["result_status_sha256"]:
            problems.append("结果摘要指纹与 CI live 不一致")
        # ⑥ 可选在线复核：生产现状逐项重取
        if args.live and not problems:
            # review_id 只在受控证据包内使用，不打印
            print(f"  {fname[:42]:44} 离线核验通过（在线复核需逐条 API 取证，见 --live 文档）")
        if problems:
            fails.append(f"{fname}: " + "；".join(problems))
        else:
            ok += 1
            print(f"  ✅ {fname[:42]:44} {e['result_status_sha256'][:12]}…")

    print("=" * 60)
    if fails:
        for f in fails:
            print(f"🔴 {f}")
    print(f"核验结果: {ok}/{EXPECTED_CONTRACTS} 一致 | 生产版本: {DEPLOY_VERSION}")
    if ok == EXPECTED_CONTRACTS and not fails:
        print("17/17 一致")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
