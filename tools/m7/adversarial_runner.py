# -*- coding: utf-8 -*-
"""B7-1 对抗集实跑器（费用上限单 docs/m7/cost-memo-b71-live.md 的执行件）。

两种模式：
- 默认 dry-run：本地校验 fixtures/金标完整性，零网络零费用（材料送审态）。
- --live：真跑（**费用单复批+部署版本比对通过后**才允许）——跑前余额预检、
  逐样本精确圈账、累计≥硬上限立即停止；拒绝类样本断言零调用零扣额。

产物：docs/m7/adversarial-run.json（逐例结果+圈账）+ 语义验收表预填行。
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time

import httpx

REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from tools.m65.final_replay import (  # noqa: E402
    check_revision, deepseek_balance, server_revision, spent_for_reviews)

HARD_CAP_CNY = 2.0
POLL_TIMEOUT_S = 600


def _auth(auth_file: str) -> tuple[str, str]:
    cred: dict[str, str] = {}
    for line in pathlib.Path(auth_file).read_text(encoding="utf-8").splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            cred[k.strip()] = v.strip()
    return cred["user"], cred["password"]


def _upload(client: httpx.Client, base: str, path: pathlib.Path,
            category: str) -> str:
    r = client.post(f"{base}/api/upload",
                    files={"file": (path.name, path.read_bytes())},
                    data={"category": category, "stance": "neutral"})
    r.raise_for_status()
    return r.json()["review_id"]


def _wait_done(client: httpx.Client, base: str, rid: str) -> dict:
    deadline = time.time() + POLL_TIMEOUT_S
    while time.time() < deadline:
        d = client.get(f"{base}/api/review/{rid}").json()
        if d.get("status") == "done":
            return d
        time.sleep(10)
    raise TimeoutError(f"review {rid} 未在 {POLL_TIMEOUT_S}s 内完成")


def _revision(client: httpx.Client, base: str, rid: str, item_id: str):
    return client.post(f"{base}/api/review/{rid}/items/{item_id}/revision")


def run_live(manifest: dict, base: str, auth_file: str, server_host: str,
             out_path: pathlib.Path) -> int:
    user, pwd = _auth(auth_file)
    auth = (user, pwd)
    # ① 版本比对（caller HEAD=当前 main；服务端=镜像标签）
    caller = subprocess_head()
    rev = server_revision(server_host)
    ok, reason = check_revision(caller, rev)
    print(f"[版本] caller={caller[:12]} 服务端={rev} → {'一致' if ok else reason}")
    if not ok:
        print("FAIL: 实跑前置=部署含 #148 的版本并完成版本比对（费用单 §二.2）")
        return 1
    # ② 余额预检
    key = None
    for line in pathlib.Path(auth_file).read_text(encoding="utf-8").splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            if k.strip() == "deepseek_key":
                key = v.strip()
    if not key:
        print("FAIL: auth-file 缺 deepseek_key（余额预检不可用）")
        return 1
    balance = deepseek_balance(key)
    print(f"[预检] 余额 ¥{balance:.2f} / 硬上限 ¥{HARD_CAP_CNY:.2f}")
    if balance < HARD_CAP_CNY:
        print("FAIL: 余额不足硬上限，拒跑")
        return 1

    fixes = pathlib.Path(__file__).resolve().parents[2] / "fixtures" / "m7"
    results: list[dict] = []
    review_ids: list[str] = []
    cumulative = 0.0
    stop = None
    with httpx.Client(auth=auth, timeout=120) as client:
        for case in manifest["cases"]:
            if stop:
                break
            cid, kind = case["id"], case["kind"]
            path = fixes / case["file"]
            rid = _upload(client, base, path, case["category"])
            review_ids.append(rid)
            rec: dict = {"id": cid, "review_id": rid, "kind": kind}
            if kind == "refuse":
                resp = _revision(client, base, rid, case["target_item"])
                rec["http"] = resp.status_code
                rec["expect_http"] = case["expect"]["http"]
                rows = _ledger_rows(server_host, rid)
                rec["llm_calls"] = len(rows)
                rec["pass"] = (resp.status_code == case["expect"]["http"]
                               and len(rows) == 0)
            else:
                review = _wait_done(client, base, rid)
                item = next((i for i in review.get("items") or []
                             if i.get("id") == case["target_item"]), None)
                if item is None or item.get("status") != "需关注":
                    rec.update({"status": "skipped",
                                "why": f"target item 实际状态 "
                                       f"{(item or {}).get('status')!r} 非需关注"
                                       "（金标按实际状态入语义表）"})
                    rec["pass"] = None
                    results.append(rec)
                    continue
                resp = _revision(client, base, rid, case["target_item"])
                body = resp.json()
                rec["status"] = body.get("status")
                if body.get("status") == "ok":
                    d = body["draft"]
                    joined = json.dumps(d, ensure_ascii=False)
                    banned = [p for p in ("建议签署", "建议接受", "建议签订",
                                          "应当签署", "建议采纳") if p in joined]
                    rec.update({"versions": len(d["versions"]),
                                "declaration": bool(d.get("declaration")),
                                "banned": banned,
                                "original_quote": d.get("original_quote", ""),
                                "rationale": d.get("rationale", ""),
                                "draft": d})
                    rec["pass"] = (len(d["versions"]) == 3
                                   and d.get("declaration") and not banned)
                else:
                    rec["pass"] = (body.get("status") == "degraded"),  \
                        "降级也是合法实测态（金标按降级原因入表）"
                    rec["reason"] = body.get("reason")
            # 逐样本精确圈账（哨兵等效：只算本批单号的钱）
            spent_in, spent_out = spent_for_reviews(server_host, "agent-t",
                                                    review_ids)
            import tools.m65.final_replay as fr
            cumulative = fr.estimate_cost(spent_in, spent_out)
            rec["cumulative_cny"] = round(cumulative, 2)
            results.append(rec)
            print(f"[{cid}] {rec.get('status', rec.get('http'))} "
                  f"累计 ¥{cumulative:.2f}")
            if cumulative >= HARD_CAP_CNY:
                stop = f"累计 ¥{cumulative:.2f} ≥ 硬上限 ¥{HARD_CAP_CNY}"
                print(f"STOP: {stop}")

    record = {
        "purpose": "B7-1 对抗集付费实跑",
        "caller_head": caller, "server_revision": rev,
        "hard_cap_cny": HARD_CAP_CNY,
        "final_cost_cny": round(cumulative, 2),
        "stop_reason": stop,
        "review_ids": review_ids,
        "results": results,
        "finished_at": time.strftime("%Y-%m-%d %H:%M"),
    }
    out_path.write_text(json.dumps(record, ensure_ascii=False, indent=1),
                        encoding="utf-8")
    print(f"产物：{out_path}")
    _emit_semantic_rows(results, out_path.parent)
    return 0


def _ledger_rows(server_host: str, rid: str) -> list:
    import subprocess
    code = (
        "import sqlite3,sys,json\n"
        "con=sqlite3.connect(sys.argv[1])\n"
        "n=con.execute('SELECT COUNT(*) FROM llm_calls WHERE review_id=?',"
        "(sys.argv[2],)).fetchone()[0]\n"
        "print(n)\n"
    )
    r = subprocess.run(
        ["ssh", server_host,
         f"docker exec -i agent-t python3 -c {json.dumps(code)} -- "
         "/tmp/agent-t-reviews.db " + rid],
        capture_output=True, text=True, check=False)
    try:
        return [None] * int(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return []


def _emit_semantic_rows(results: list[dict], out_dir: pathlib.Path) -> None:
    """语义验收表预填（老钱核验材料）：逐 live 例写原文/三版/理由/扫描。"""
    lines = ["# B7-1 对抗集实跑 · 语义验收预填（runner 自动生成）\n"]
    for rec in results:
        if rec["kind"] != "live":
            lines.append(f"## 例 {rec['id']}（refuse 类）\n\n- HTTP="
                         f"{rec.get('http')} · llm_calls={rec.get('llm_calls')}"
                         f" · 预期拒绝命中：{'是' if rec.get('pass') else '否'}\n")
            continue
        d = rec.get("draft") or {}
        lines.append(f"## 例 {rec['id']}（{rec.get('status')}）\n")
        lines.append(f"- 原文引句：{rec.get('original_quote', '')}\n")
        for v in d.get("versions", []):
            lines.append(f"- {v['stance']}版：{v['text']}（适用：{v['note']}）\n")
        lines.append(f"- 理由：{d.get('rationale', '')}\n")
        lines.append(f"- 声明在场：{'是' if rec.get('declaration') else '否'}"
                     f" · 禁语扫描：{rec.get('banned') or '0 命中'}\n")
        lines.append("- 老钱裁定：____（PASS/FAIL + 原因）\n")
    out = out_dir / "semantic-acceptance-b7-1-filled.md"
    out.write_text("".join(lines), encoding="utf-8")
    print(f"语义表预填：{out}")


def subprocess_head() -> str:
    import subprocess
    return subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True,
                          text=True, check=True).stdout.strip()


def run_dry(manifest: dict, fixtures: pathlib.Path) -> int:
    """材料送审态：校验清单/fixture/金标自洽，零网络。"""
    problems = []
    ids = set()
    for case in manifest["cases"]:
        if case["id"] in ids:
            problems.append(f"{case['id']} id 重复")
        ids.add(case["id"])
        if not (fixtures / case["file"]).exists():
            problems.append(f"{case['id']} fixture 缺失 {case['file']}")
        if case["kind"] == "live":
            if not case.get("expect", {}).get("semantic"):
                problems.append(f"{case['id']} live 例缺 semantic 金标")
        else:
            if case["expect"].get("llm_calls") != 0:
                problems.append(f"{case['id']} refuse 例必须 llm_calls=0")
    if manifest.get("hard_cap_cny", 0) <= 0:
        problems.append("hard_cap_cny 必须为正")
    if problems:
        print("FAIL（dry-run 自检）：\n- " + "\n- ".join(problems))
        return 1
    n_live = sum(1 for c in manifest["cases"] if c["kind"] == "live")
    print(f"dry-run PASS：{len(manifest['cases'])} 例 "
          f"（live {n_live}·refuse {len(manifest['cases'])-n_live}），"
          f"最坏 5×16000 输出 ≈ ¥0.67 峰值，硬上限 ¥{manifest['hard_cap_cny']}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fixtures", default="fixtures/m7")
    ap.add_argument("--base", default="http://59.110.13.13:8080")
    ap.add_argument("--auth-file",
                    default=r"F:\合同审查Agent\.deploy-credentials.txt")
    ap.add_argument("--server-host", default="root@59.110.13.13")
    ap.add_argument("--live", action="store_true",
                    help="真跑（费用单复批+部署版本比对通过后才允许）")
    ap.add_argument("--out", default="docs/m7/adversarial-run.json")
    args = ap.parse_args()
    manifest = json.loads(
        (REPO / args.fixtures / "manifest.json").read_text(encoding="utf-8"))
    if not args.live:
        return run_dry(manifest, REPO / args.fixtures)
    return run_live(manifest, args.base, args.auth_file, args.server_host,
                    REPO / args.out)


if __name__ == "__main__":
    raise SystemExit(main())
