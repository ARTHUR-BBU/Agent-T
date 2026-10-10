# -*- coding: utf-8 -*-
"""生产对照 · 并发版（用户点名并发化；替代历轮临时对照脚本）。

部署后把 17 份真实合同并发上传生产，逐份轮询至 done，收集状态表并与
CI 门禁基线（docs/m65/batch1-regression.json 的 live 状态表）比对。

用法（凭据文件键 url/user/password；值只进内存不打印）：
  python -X utf8 tools/m65/prod_regression.py \
      --credentials "F:/合同审查Agent/.deploy-credentials.txt" \
      [--workers 4] [--out rid 落盘路径]

放行口径：全部 17 份 done 且状态表与 CI 基线逐项相等 → 退出码 0。
预期内的部署 diff（如新词表翻转）须先更新 CI 基线（门禁重跑）再对照。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[2]
_CRED_KEYS = ("url", "user", "password")


def _load_credentials(path: Path) -> tuple[str, tuple[str, str]]:
    """凭据只进内存；URL 原样使用（只去尾 /），只接受 http/https。"""
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
        raise ValueError("凭据 url 协议必须是 http:// 或 https://")
    return url, (cred["user"], cred["password"])


def _upload_and_wait(
    base: str, auth: tuple[str, str], fname: str, category: str,
    poll_interval: float, timeout_s: float,
) -> dict:
    """单份：并发上传 + 轮询至 done；失败留痕不中断其他份。"""
    entry: dict = {"file": fname, "category": category, "rid": None,
                   "status": None, "items": None, "error": None}
    try:
        raw = (ROOT / "fixtures-real" / fname).read_bytes()
        r = requests.post(
            f"{base}/api/upload", auth=auth,
            files={"file": (fname, raw, "application/octet-stream")},
            data={"category": category, "force": "true"}, timeout=180)
        if r.status_code != 200:
            entry["error"] = f"上传 HTTP {r.status_code}: {r.text[:120]}"
            return entry
        rid = r.json()["review_id"]
        entry["rid"] = rid
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            d = requests.get(f"{base}/api/review/{rid}", auth=auth, timeout=60).json()
            if d.get("status") == "done":
                entry["status"] = "done"
                entry["items"] = {i["id"]: i["status"] for i in d["items"]}
                return entry
            if d.get("status") in ("error", "failed"):
                entry["error"] = f"审查失败: {d.get('status')}"
                return entry
            time.sleep(poll_interval)
        entry["error"] = f"轮询超时 {timeout_s}s（rid 已留盘可续查）"
    except Exception as exc:  # noqa: BLE001
        entry["error"] = f"{type(exc).__name__}: {exc}"
    return entry


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--credentials", required=True)
    ap.add_argument("--workers", type=int, default=4,
                    help="并发上传数（默认 4；过高会触发限频）")
    ap.add_argument("--poll-interval", type=float, default=4.0)
    ap.add_argument("--timeout", type=float, default=600.0,
                    help="单份轮询超时秒")
    ap.add_argument("--out", help="rid 落盘路径（受控目录，不入仓库）")
    args = ap.parse_args(argv)

    base, auth = _load_credentials(Path(args.credentials))
    manifest = json.loads(
        (ROOT / "fixtures-real" / "manifest.json").read_text(encoding="utf-8"))
    ci_live = {r["file"]: r["live"] for r in json.loads(
        (ROOT / "docs" / "m65" / "batch1-regression.json")
        .read_text(encoding="utf-8"))["rows"]}

    print(f"生产对照：{len(manifest)} 份，并发 {args.workers}，基线=batch1-regression live")
    results: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = {
            pool.submit(_upload_and_wait, base, auth, fname, category,
                        args.poll_interval, args.timeout): fname
            for fname, category in manifest.items()
        }
        for fut in as_completed(futs):
            entry = fut.result()
            results[entry["file"]] = entry
            mark = "✅" if entry["error"] is None else "🔴"
            print(f"  {mark} {entry['file'][:40]:42} "
                  f"rid={str(entry['rid'])[:12]} {entry['error'] or 'done'}")

    if args.out:
        Path(args.out).write_text(
            json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"rid 明细已落盘: {args.out}")

    fails: list[str] = []
    for fname in manifest:
        e = results.get(fname, {})
        if e.get("error"):
            fails.append(f"{fname}: {e['error']}")
            continue
        ci = ci_live.get(fname, {})
        live = e["items"] or {}
        delta = {k: (ci.get(k), live.get(k))
                 for k in ci.keys() | live.keys() if ci.get(k) != live.get(k)}
        if delta:
            fails.append(f"{fname}: 与 CI 基线有差异 {delta}")

    print("=" * 66)
    if fails:
        for f in fails:
            print(f"🔴 {f}")
    print(f"对照结果: {len(manifest) - len(fails)}/{len(manifest)} 与 CI 基线一致")
    if not fails:
        print(f"{len(manifest)}/{len(manifest)} 一致")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
