"""M6.5 真实合同验证 · 七问评测跑道。

用法：python -X utf8 tools/m65/run_eval.py --fixtures fixtures-real --out docs/m65/<run>.json

对 fixtures-real/ 里的真实合同范本，走**生产 API** 全链路实测
（上传→预审→核查→质量→异议→核验→追问→报告导出），按「七问」记录指标：

  1 找风险准不准   —— 需关注项数 + 全量条目明细落盘（供人工判准）
  2 证据可信吗     —— 主张编号覆盖率 / 证据核验状态分布 / 坏引用数
  3 AI解释有帮助吗 —— 质量观察数 + 追问回答完整度（字段齐备率）
  4 看得懂吗       —— 条目 note/quote/追问答案原文留档（供人工通读）
  5 省时间吗       —— 每份合同端到端耗时 vs 合同字数
  6 失败诚实吗     —— 追问「未定位原文」诚实拒答率 / error 可用性旗标
  7 成本速度可接受 —— 全程总耗时 + 每份耗时分布

凭据红线：Basic Auth 账号密码只从本地凭据文件读、只进请求头，绝不打印。
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]

# 品类识别：按文件名关键词路由到系统支持的三品类
_CATEGORY_HINTS = [
    ("procurement", "procurement"),
    ("采购", "procurement"),
    ("lease", "lease"),
    ("租赁", "lease"),
    ("nda", "nda"),
    ("保密", "nda"),
]

POLL_INTERVAL_S = 10
POLL_TIMEOUT_S = 900  # 15 分钟（真实长合同 + 限频）
ASKS_PER_CONTRACT = 2


def detect_category(fname: str, manifest: dict) -> str:
    if fname in manifest:
        return str(manifest[fname])
    low = fname.lower()
    for hint, cat in _CATEGORY_HINTS:
        if hint in low:
            return cat
    return "procurement"


def load_auth_header(auth_file: Path) -> str:
    """从凭据文件读 user/password 拼 Basic 头；值不打印、不落日志。"""
    vals: dict[str, str] = {}
    if auth_file.exists():
        for ln in io.open(auth_file, encoding="utf-8"):
            ln = ln.strip()
            if ln and not ln.startswith("#") and "=" in ln:
                k, v = ln.split("=", 1)
                vals[k.strip()] = v.strip()
    user, password = vals.get("user", ""), vals.get("password", "")
    if not user or not password:
        print(f"FATAL: 凭据文件 {auth_file} 缺 user/password", file=sys.stderr)
        sys.exit(2)
    token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def upload_contract(client: httpx.Client, base: str, path: Path, category: str) -> str:
    """上传 → 拿 review_id。category_confirm 分支用 force 重传（评测需直接出结果）。"""
    with io.open(path, "rb") as fh:
        r = client.post(
            f"{base}/api/upload",
            files={"file": (path.name, fh)},
            data={"category": category, "force": "true"},
            timeout=120,
        )
    r.raise_for_status()
    data = r.json()
    rid = data.get("review_id")
    if not rid:
        raise RuntimeError(f"upload 未返回 review_id: {data.get('status')} {data.get('message')}")
    return str(rid)


def poll_review(client: httpx.Client, base: str, rid: str) -> dict:
    t0 = time.time()
    while time.time() - t0 < POLL_TIMEOUT_S:
        r = client.get(f"{base}/api/review/{rid}", timeout=60)
        r.raise_for_status()
        row = r.json()
        if row.get("status") in ("done", "error"):
            return row
        time.sleep(POLL_INTERVAL_S)
    raise TimeoutError(f"{rid} 轮询超时 {POLL_TIMEOUT_S}s")


def run_asks(client: httpx.Client, base: str, rid: str, row: dict, fname: str) -> list[dict]:
    targets = [i for i in (row.get("items") or [])
               if i.get("status") == "需关注" and (i.get("quote") or "").strip()]
    out: list[dict] = []
    for item in targets[:ASKS_PER_CONTRACT]:
        t0 = time.time()
        try:
            r = client.post(
                f"{base}/api/ask",
                json={"review_id": rid, "item_id": item.get("id"),
                      "question": "这条条款的风险是什么？请引用原句说明。"},
                timeout=180,
            )
            dt = round(time.time() - t0, 1)
            r.raise_for_status()
            data = r.json()
            ans = data.get("answer") or {}
            model_quote = ""
            try:
                model_quote = str((json.loads(data.get("raw_text") or "{}")).get("原文在哪") or "").strip()
            except json.JSONDecodeError:
                pass
            out.append({
                "file": fname,
                "item_id": item.get("id"),
                "item_name": item.get("name"),
                "seconds": dt,
                "ok": bool(data.get("ok")),
                "parse_ok": bool(str(ans.get("问题是啥") or "").strip()),
                "fields_filled": sum(1 for f in ("风险等级", "这条在查啥", "问题是啥", "建议怎么改")
                                     if str(ans.get(f) or "").strip()),
                "quote_verified": data.get("quote_verified"),
                "evidence_verification": (data.get("evidence") or {}).get("verification"),
                # 诚实度从核验前的原始模型输出测（核验器会覆写「原文在哪」）
                "honest_decline": (not model_quote) or ("未定位" in model_quote),
                # 看得懂吗：留全文供人工通读
                "answer_text": {f: str(ans.get(f) or "") for f in
                                ("风险等级", "这条在查啥", "问题是啥", "建议怎么改")},
            })
        except Exception as exc:  # noqa: BLE001
            out.append({"file": fname, "item_id": item.get("id"),
                        "ok": False, "error": type(exc).__name__,
                        "seconds": round(time.time() - t0, 1)})
    return out


def run_unsupported_probe(client: httpx.Client, base: str, rid: str,
                          row: dict, fname: str) -> dict | None:
    """无证据探针：挑一个「未找到」类目（本条无摘句可引）追问——模型面前
    没有可用证据，此刻拒答/声明未定位才是真·诚实拒答。没有合适目标返回 None。"""
    target = next((i for i in (row.get("items") or []) if i.get("status") == "未找到"), None)
    if not target:
        return None
    t0 = time.time()
    try:
        r = client.post(
            f"{base}/api/ask",
            json={"review_id": rid, "item_id": target.get("id"),
                  "question": "这条条款的风险是什么？请引用原句说明。"},
            timeout=180,
        )
        r.raise_for_status()
        data = r.json()
        model_quote = ""
        try:
            model_quote = str((json.loads(data.get("raw_text") or "{}")).get("原文在哪") or "").strip()
        except json.JSONDecodeError:
            pass
        return {
            "file": fname, "item_id": target.get("id"), "item_name": target.get("name"),
            "seconds": round(time.time() - t0, 1), "ok": bool(data.get("ok")),
            # 无证据时编造引文 = 不诚实；拒答/「未定位」 = 诚实
            "fabricated_quote": bool(model_quote) and "未定位" not in model_quote,
            "model_quote_preview": model_quote[:120],
        }
    except Exception as exc:  # noqa: BLE001
        return {"file": fname, "item_id": target.get("id"),
                "ok": False, "error": type(exc).__name__,
                "seconds": round(time.time() - t0, 1)}


def eval_contract(client: httpx.Client, base: str, path: Path, category: str) -> dict:
    text_chars_full = 0
    rec: dict = {"file": path.name, "category": category}
    try:
        rid = upload_contract(client, base, path, category)
        rec["review_id"] = rid
        row = poll_review(client, base, rid)
        rec["status"] = row.get("status")
        rec["error"] = row.get("error")
        # 合同字数（Q5 分母）：P2（外审 #86）——text_preview 被 API 截到 500 字，
        # 不能当全长；改用条款索引逐条 chars 求和（服务端真实解析长度）
        clause_index = row.get("clause_index") or {}
        text_chars_full = sum(int(c.get("chars") or 0) for c in (clause_index.get("clauses") or []))
        items = row.get("items") or []

        attention = [i for i in items if i.get("status") == "需关注"]
        # Q2 证据可信吗
        with_claim = [i for i in items if (i.get("claim_id") or "").strip()]
        verif_dist: dict[str, int] = {}
        for i in items:
            ev = i.get("evidence") or {}
            v = str(ev.get("verification") or "none")
            verif_dist[v] = verif_dist.get(v, 0) + 1
        registry = row.get("evidence_registry") or {}

        rec["metrics"] = {
            # Q1 找风险准不准（明细在 items_detail 供人工判准）
            "items_total": len(items),
            "attention_items": len(attention),
            "scorecard_available": bool((row.get("scorecard") or {}).get("available")),
            "scorecard_total": (row.get("scorecard") or {}).get("total"),
            # Q2 证据可信吗
            "claim_id_rate": round(len(with_claim) / len(items), 2) if items else None,
            "evidence_verification_dist": verif_dist,
            "broken_ref_count": registry.get("broken_ref_count"),
            "unique_evidence": registry.get("unique_total"),
            # Q3 AI 解释有帮助吗
            "quality_available": bool((row.get("quality") or {}).get("available")),
            "quality_reason": (row.get("quality") or {}).get("reason"),
            "quality_observations": len((row.get("quality") or {}).get("observations") or []),
            "facts": len(row.get("facts") or []),
            "objections_available": bool((row.get("objections") or {}).get("available")),
            "objections_count": len((row.get("objections") or {}).get("objections") or []),
            # Q6 失败诚实吗
            "completion": row.get("completion"),
        }
        # Q4 看得懂吗：条目全量明细留档（人工通读判准用）
        rec["items_detail"] = [
            {"id": i.get("id"), "name": i.get("name"), "status": i.get("status"),
             "note": (i.get("note") or "")[:300], "quote": (i.get("quote") or "")[:200],
             "claim_id": i.get("claim_id"),
             "evidence_verification": (i.get("evidence") or {}).get("verification")}
            for i in items
        ]
        # 核验层只读快照（Q1/Q6 参考）
        verify = row.get("verify") or {}
        rec["verify"] = {
            "available": bool(verify.get("available")),
            "questions_total": len(verify.get("questions") or []),
            "pending": sum(1 for q in (verify.get("questions") or []) if q.get("status") == "pending"),
        }
        # Q3/Q4 追问实测
        rec["asks"] = run_asks(client, base, rid, row, path.name)
        # Q6 无证据探针（P2 外审 #86）：拿一个「未找到」类目（无摘句）追问——
        # 模型面前没有可用证据，此时拒答/声明未定位才是真·诚实拒答；
        # 有摘句目标上的 honest_decline 只测「有证据会不会引」，两者分开记
        rec["unsupported_probe"] = run_unsupported_probe(client, base, rid, row, path.name)
        # 报告可交付性（顺带验证 docx 导出在真实合同上不炸）
        try:
            r = client.get(f"{base}/api/review/{rid}/report", timeout=120)
            rec["report"] = {"http": r.status_code, "bytes": len(r.content),
                             "is_docx": r.content[:2] == b"PK"}
        except Exception as exc:  # noqa: BLE001
            rec["report"] = {"error": type(exc).__name__}
    except Exception as exc:  # noqa: BLE001
        rec["error"] = f"{type(exc).__name__}: {exc}"
    rec["text_chars_full"] = text_chars_full
    return rec


def summarize(results: list[dict], wall_seconds: float) -> dict:
    done = [r for r in results if r.get("status") == "done"]
    # P1（外审 #86）：失败请求进分母——先筛 ok 再统计会把失败藏掉
    # （像只统计交卷的人）。attempted = 全部尝试，failed = 异常/非 ok 请求
    asks_all = [a for r in results for a in (r.get("asks") or [])]
    ok_asks = [a for a in asks_all if a.get("ok")]
    reports = [r.get("report") or {} for r in results]
    return {
        "contracts_total": len(results),
        "contracts_done": len(done),
        "ask_attempted": len(asks_all),
        "ask_ok": len(ok_asks),
        "ask_failed": len(asks_all) - len(ok_asks),
        "ask_parse_ok": sum(1 for a in ok_asks if a.get("parse_ok")),
        "ask_fields_filled_avg": (round(sum(a.get("fields_filled") or 0 for a in ok_asks)
                                        / len(ok_asks), 1)
                                  if ok_asks else None),
        "ask_honest_decline": sum(1 for a in ok_asks if a.get("honest_decline")),
        "ask_quote_verified": sum(1 for a in ok_asks if a.get("quote_verified")),
        "claim_id_rates": [r["metrics"].get("claim_id_rate") for r in done if r.get("metrics")],
        "report_ok": sum(1 for x in reports if x.get("is_docx")),
        "wall_seconds": round(wall_seconds, 1),
    }


def require_tls(base: str) -> bool:
    """HTTPS 或回环地址才允许携带 Basic 凭据；其余明文地址须显式 --insecure。"""
    if base.startswith("https://"):
        return True
    try:
        host = base.split("://", 1)[1].split("/", 1)[0].split(":", 1)[0].lower()
    except IndexError:
        return False
    return host in ("localhost", "127.0.0.1", "::1")


def select_contract_files(fixtures_dir: Path, manifest: dict,
                          include_all: bool = False) -> tuple[list[Path], list[Path]]:
    """P1（外审 #86）：manifest 是唯一「正式合同」名册——不在名册里的文件
    （如资料汇编 PDF）默认不跑，防止混进合同统计；--all 才作为额外场景
    （extra-non-contract）附带执行，单独落 contracts_extra，不入七问汇总。"""
    supported = (".docx", ".doc", ".pdf", ".txt")
    on_disk = sorted(p for p in fixtures_dir.iterdir() if p.suffix.lower() in supported)
    in_manifest = [fixtures_dir / name for name in sorted(manifest)
                   if (fixtures_dir / name).exists()]
    extras = [p for p in on_disk if p not in in_manifest]
    return in_manifest, (extras if include_all else [])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://59.110.13.13:8080", help="生产 API 地址")
    ap.add_argument("--fixtures", default="fixtures-real", help="真实合同目录")
    ap.add_argument("--out", default="docs/m65/run.json")
    ap.add_argument("--auth-file", default=r"F:\合同审查Agent\.deploy-credentials.txt")
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 份（0=全部）")
    ap.add_argument("--all", action="store_true",
                    help="附带执行 manifest 外文件（需 manifest 显式品类；单独落 contracts_extra，不入七问汇总）")
    ap.add_argument("--insecure", action="store_true",
                    help="允许向非 HTTPS 非回环地址发送 Basic 凭据与合同全文（自担风险）")
    args = ap.parse_args()

    fixtures = ROOT / args.fixtures
    manifest_path = fixtures / "manifest.json"
    manifest = (json.loads(io.open(manifest_path, encoding="utf-8").read())
                if manifest_path.exists() else {})
    files, extra_files = select_contract_files(fixtures, manifest, include_all=args.all)
    missing = [n for n in sorted(manifest) if not (fixtures / n).exists()]
    for name in missing:
        print(f"[m65] WARN: manifest 内文件缺失磁盘：{name}", file=sys.stderr)
    if args.limit:
        files = files[: args.limit]
        extra_files = extra_files[: args.limit]
    if not files:
        print(f"FATAL: {fixtures} 无 manifest 名册内合同文件", file=sys.stderr)
        sys.exit(2)
    # P1（外审 #86）：Basic 凭据+合同全文不许走明文 HTTP 到非回环地址——
    # 生产 HTTPS 待备案，内网/本机调试必须显式 --insecure 自担风险
    if not require_tls(args.base) and not args.insecure:
        print(f"FATAL: {args.base} 非 HTTPS 且非回环地址——Basic 凭据与合同全文会明文上网。"
              f"确认风险后加 --insecure 重跑", file=sys.stderr)
        sys.exit(2)

    auth = load_auth_header(Path(args.auth_file))
    headers = {"Authorization": auth}
    results: list[dict] = []
    extras: list[dict] = []
    wall0 = time.time()
    with httpx.Client(headers=headers) as client:
        # 连通性预检（不打凭据日志）
        r = client.get(f"{args.base}/api/categories", timeout=30)
        r.raise_for_status()
        print(f"connected {args.base} | categories={len(r.json())}", flush=True)

        for p in files:
            cat = detect_category(p.name, manifest)
            print(f"[m65] {p.name} -> {cat} ...", flush=True)
            rec = eval_contract(client, args.base, p, cat)
            m = rec.get("metrics") or {}
            print(f"      status={rec.get('status')} 需关注 {m.get('attention_items')}/{m.get('items_total')}"
                  f" | claim_id率 {m.get('claim_id_rate')} | quality={m.get('quality_available')}"
                  f" | asks={len(rec.get('asks') or [])}", flush=True)
            results.append(rec)
        # manifest 外文件 = 非合同场景（资料汇编等），只留档不进七问汇总；
        # P1（外审 #86）：附加场景同样不许静默猜品类（默认采购会塞错规则包）
        for p in extra_files:
            cat = manifest.get(p.name)
            if not cat:
                print(f"[m65][extra] 跳过 {p.name}：名册外文件无显式品类，"
                      f"不猜（防错规则包）。需要的请在 manifest 登记品类后重跑", file=sys.stderr)
                continue
            print(f"[m65][extra] {p.name} -> {cat}（非合同场景，不入汇总）...", flush=True)
            rec = eval_contract(client, args.base, p, cat)
            rec["scenario"] = "extra-non-contract"
            extras.append(rec)
            m = rec.get("metrics") or {}
            print(f"      status={rec.get('status')} 需关注 {m.get('attention_items')}/{m.get('items_total')}",
                  flush=True)
    wall = time.time() - wall0

    out = {"base": args.base, "files": [p.name for p in files],
           "summary": summarize(results, wall), "contracts": results}
    if extras:
        out["contracts_extra"] = extras
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    io.open(out_path, "w", encoding="utf-8").write(json.dumps(out, ensure_ascii=False, indent=2))
    print(f"[m65] summary: {json.dumps(out['summary'], ensure_ascii=False)}")
    if extras:
        print(f"[m65] extras（不入汇总）: {[e['file'] for e in extras]}")
    print(f"saved -> {out_path}")


if __name__ == "__main__":
    main()
