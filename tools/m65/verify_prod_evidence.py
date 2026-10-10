"""生产证据闭环 · 只读核验脚本（外审施工卡③；PR #93 三 P1 销项版）。

用法：
  python -X utf8 tools/m65/verify_prod_evidence.py \
      --evidence "F:/合同审查Agent/audit-evidence/prod-evidence-5e93969.json" \
      [--credentials "F:/合同审查Agent/.deploy-credentials.txt"]   # --live 必需

核验口径（放行标准：零退出 + 输出 17/17 一致 + 生产版本明确）：
  0. 证据包文件 SHA256 == docs/m65/prod-evidence-summary.json 固定的
     evidence_pkg_sha256（P1 销项：包本身先验封条，防篡改后重算指纹蒙混）
  1. 证据条数 == 17 且 filename 集合 == manifest 键集合（P1 销项：防缺漏/
     重复凑数——只验数量会漏「少一份 + 重复一份仍 17 条」）
  2. 每条合同文件 SHA256 与本地 fixtures-real 实算一致（文件未漂移）
  3. 合同类型与 fixtures-real/manifest.json 一致
  4. 部署版本 == DEPLOY_VERSION（5e93969）
  5. 结果摘要指纹 == 按 batch1-regression.json 的 CI live 状态表规范 JSON
     重算的 SHA256（生产审查结果与 CI 门禁验证过的状态逐项一致）
  6. --live（可选）：读本地凭据文件连生产 API 逐条重取检查项状态，
     与 CI live 状态表比对（在线复核证据包取证后生产未再变化）
本脚本只读：不写任何文件，不改任何数据。凭据只进内存，不打印。
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[2]
DEPLOY_VERSION = "5e93969"
EXPECTED_CONTRACTS = 17
# 凭据文件键名（值永不打印）
_CRED_KEYS = ("url", "user", "password")


def _status_digest(status_map: dict[str, str]) -> str:
    """结果摘要指纹（与证据包导出口径同源：规范 JSON 的 SHA256）。"""
    src = json.dumps(status_map, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(src.encode("utf-8")).hexdigest()


def _load_credentials(path: Path) -> tuple[str, tuple[str, str]]:
    """读凭据文件 → (BASE_URL, basic auth)。只进内存。

    URL 保全红线（外审 P1）：保留凭据文件的完整 URL（只去末尾 /），
    不得重写协议或端口——隐式降级到 http://host:8080 会在启用 HTTPS/
    反向代理/非默认端口时连错服务，并以明文发送 Basic Auth。
    只接受 http/https，其他协议立即失败。"""
    cred: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            if k.strip() in _CRED_KEYS:
                cred[k.strip()] = v.strip()
    missing = [k for k in _CRED_KEYS if k not in cred]
    if missing:
        raise ValueError(f"凭据文件缺键: {missing}")
    url = cred["url"].strip().rstrip("/")
    if not (url.startswith("http://") or url.startswith("https://")):
        raise ValueError("凭据 url 协议必须是 http:// 或 https://（拒绝隐式降级或陌生协议）")
    return url, (cred["user"], cred["password"])


def live_refetch(entries: list[dict], base: str, auth: tuple[str, str],
                 fails: list[str]) -> dict[str, dict[str, str]]:
    """逐条 GET 生产审查记录，返回 file -> 状态表；异常记入 fails。"""
    live: dict[str, dict[str, str]] = {}
    for e in entries:
        r = requests.get(f"{base}/api/review/{e['review_id']}", auth=auth, timeout=60)
        if r.status_code != 200:
            fails.append(f"{e['filename']}: 在线复核 HTTP {r.status_code}")
            continue
        d = r.json()
        if d.get("status") != "done":
            fails.append(f"{e['filename']}: 生产现状 status={d.get('status')} ≠ done")
            continue
        live[e["filename"]] = {i["id"]: i["status"] for i in d["items"]}
    return live


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--evidence", required=True, help="受控证据包路径（含 review_id 明细，不入仓库）")
    ap.add_argument("--live", action="store_true", help="连生产 API 逐条在线复核（需 --credentials）")
    ap.add_argument("--credentials", help="凭据文件路径（--live 必需；键 url/user/password）")
    args = ap.parse_args(argv)

    fails: list[str] = []
    ev_path = Path(args.evidence)

    # ⓪ 证据包自身封条（P1 销项：入库 summary 固定了包哈希，先验封条）
    summary = json.loads((ROOT / "docs" / "m65" / "prod-evidence-summary.json")
                         .read_text(encoding="utf-8"))
    expect_pkg_sha = summary["evidence_pkg_sha256"]
    actual_pkg_sha = hashlib.sha256(ev_path.read_bytes()).hexdigest()
    if actual_pkg_sha != expect_pkg_sha:
        fails.append(f"证据包 SHA256 不匹配：实际 {actual_pkg_sha[:16]}… ≠ 固定 {expect_pkg_sha[:16]}…"
                     f"（包已被篡改或换包——后续核验结果不可信）")

    pkg = json.loads(ev_path.read_text(encoding="utf-8"))
    entries = pkg["entries"]
    manifest = json.loads((ROOT / "fixtures-real" / "manifest.json").read_text(encoding="utf-8"))
    ci_live = {r["file"]: r["live"] for r in json.loads(
        (ROOT / "docs" / "m65" / "batch1-regression.json").read_text(encoding="utf-8"))["rows"]}

    # ① 数量 + 文件名集合（P1 销项：防缺漏/重复凑数）
    if len(entries) != EXPECTED_CONTRACTS:
        fails.append(f"证据条数 {len(entries)} ≠ {EXPECTED_CONTRACTS}")
    ev_files = [e["filename"] for e in entries]
    dupes = sorted({f for f in ev_files if ev_files.count(f) > 1})
    if dupes:
        fails.append(f"证据包内重复条目: {dupes}")
    if set(ev_files) != set(manifest):
        fails.append(f"文件集合与盘点不一致：缺 {sorted(set(manifest) - set(ev_files))}，"
                     f"多 {sorted(set(ev_files) - set(manifest))}")
    if pkg.get("deploy_version") != DEPLOY_VERSION:
        fails.append(f"证据包版本 {pkg.get('deploy_version')} ≠ {DEPLOY_VERSION}")

    # --live：在线复核准备（凭据只进内存；URL 原样使用不降级）
    live_status: dict[str, dict[str, str]] = {}
    if args.live:
        if not args.credentials:
            fails.append("--live 需要 --credentials 凭据文件路径")
        else:
            try:
                base, auth = _load_credentials(Path(args.credentials))
                live_status = live_refetch(entries, base, auth, fails)
            except ValueError as exc:
                fails.append(str(exc))

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
        if expect_pkg_sha == actual_pkg_sha:  # 封条已破时不再逐条判（避免噪声）
            if _status_digest(ci_live[fname]) != e["result_status_sha256"]:
                problems.append("结果摘要指纹与 CI live 不一致")
        # ⑥ 在线复核：生产现状与 CI live 逐项比对
        if args.live and fname in live_status:
            if live_status[fname] != ci_live[fname]:
                delta = {k: (ci_live[fname].get(k), live_status[fname].get(k))
                         for k in ci_live[fname].keys() | live_status[fname].keys()
                         if ci_live[fname].get(k) != live_status[fname].get(k)}
                problems.append(f"生产现状与 CI live 有差异: {delta}")
        if problems:
            fails.append(f"{fname}: " + "；".join(problems))
        else:
            ok += 1
            suffix = " +在线一致" if args.live and fname in live_status else ""
            print(f"  ✅ {fname[:42]:44} {e['result_status_sha256'][:12]}…{suffix}")

    print("=" * 60)
    if fails:
        for f in fails:
            print(f"🔴 {f}")
    print(f"核验结果: {ok}/{EXPECTED_CONTRACTS} 一致 | 生产版本: {DEPLOY_VERSION}"
          + (" | 在线复核" if args.live else " | 离线核验"))
    if ok == EXPECTED_CONTRACTS and not fails:
        print("17/17 一致")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
