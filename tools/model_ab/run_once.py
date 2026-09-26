"""模型 A/B 对比跑道（单 profile 一跑）。

用法：python -X utf8 tools/model_ab/run_once.py --profile glm|deepseek --out docs/model-ab/<name>.json

同卷同码：三份已知风险设计卷（procurement/lease/nda 各一）走完整生产管线
（评分卡/补盲/质量/异议）+ 每份对「有摘录的需关注项」追问 2 次；
两 profile 唯一差异是环境变量（模型），差异全部归因于模型脾气。

凭据红线：key 只从本地文件读、只注入子进程环境，绝不打印。
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))  # 任意 cwd 直跑


def _load_env_file(path: Path) -> dict[str, str]:
    vals: dict[str, str] = {}
    if not path.exists():
        return vals
    for ln in io.open(path, encoding="utf-8"):
        ln = ln.strip()
        if ln and not ln.startswith("#") and "=" in ln:
            k, v = ln.split("=", 1)
            vals[k.strip()] = v.strip()
    return vals


def setup_profile(profile: str) -> None:
    """glm = easyrouter + glm-5.3-flash（走 ZHIPU_* 通道）；
    deepseek = 官方 API（key 从部署凭据文件读，不进对话）。"""
    for k in ("ZHIPU_API_KEY", "ZHIPU_API_BASE", "GLM_MODEL", "GLM_API_KEY",
              "DEEPSEEK_API_KEY", "DEEPSEEK_MODEL"):
        os.environ.pop(k, None)
    if profile == "glm":
        env = _load_env_file(ROOT / ".env")
        for k in ("ZHIPU_API_BASE", "ZHIPU_API_KEY", "GLM_MODEL"):
            if k in env and env[k]:
                os.environ[k] = env[k]
        if not os.environ.get("ZHIPU_API_KEY"):
            print("FATAL: .env 无 ZHIPU_API_KEY", file=sys.stderr)
            sys.exit(2)
    elif profile == "deepseek":
        creds = _load_env_file(Path(r"F:\合同审查Agent\.deploy-credentials.txt"))
        if not creds.get("deepseek_key"):
            print("FATAL: 部署凭据文件无 deepseek_key", file=sys.stderr)
            sys.exit(2)
        os.environ["DEEPSEEK_API_KEY"] = creds["deepseek_key"]
        os.environ["DEEPSEEK_MODEL"] = creds.get("deepseek_model", "")
    else:
        raise SystemExit(f"未知 profile: {profile}")


CONTRACTS = [
    ("procurement_four_risk.txt", "procurement"),
    ("lease_four_risk.txt", "lease"),
    ("nda_gold_risks.txt", "nda"),
]
ASKS_PER_CONTRACT = 2  # 每份合同最多追问数（有摘录的需关注项）


def run_contract(fname: str, category: str) -> tuple[dict, dict]:
    """跑一次完整管线；返回 (统计摘要, 管线完整产物)。"""
    from app.graph.pipeline import run_review

    content = (ROOT / "fixtures" / fname).read_bytes()
    t0 = time.time()
    result = run_review(fname, content, category=category)
    dt = round(time.time() - t0, 1)
    items = result.get("items") or []
    obs = (result.get("objections") or {})
    quality = (result.get("quality") or {})
    summary = {
        "file": fname,
        "category": category,
        "seconds": dt,
        "status": result.get("status") or ("error" if result.get("error") else "done"),
        "error": result.get("error"),
        "items_total": len(items),
        "attention_items": sum(1 for i in items if i.get("status") == "需关注"),
        "scorecard_available": bool((result.get("scorecard") or {}).get("available")),
        "scorecard_total": (result.get("scorecard") or {}).get("total"),
        "blind_available": bool(result.get("blind_enabled")),
        "blind_candidates": len(result.get("blind_candidates") or []),
        "quality_available": bool(quality.get("available")),
        "quality_reason": quality.get("reason"),
        "quality_observations": len(quality.get("observations") or []),
        "facts": len(result.get("facts") or []),
        "objections_available": bool(obs.get("available")),
        "objections_sent": (obs.get("coverage") or {}).get("sent"),
        "objections_accepted": sum(1 for o in (obs.get("objections") or []) if o.get("accepted")),
        "objections_rejected": obs.get("rejected_count"),
        "counter_states": [o.get("counter_evidence_status") for o in (obs.get("objections") or [])],
    }
    return summary, result


def run_asks(review_result: dict, fname: str) -> list[dict]:
    from app.services import llm_ask
    from app.services.clause_index import build_clause_context, build_clause_index

    text = review_result.get("text") or ""
    ci = review_result.get("clause_index") or build_clause_index(text)
    targets = [i for i in (review_result.get("items") or [])
               if i.get("status") == "需关注" and (i.get("quote") or "").strip()]
    out: list[dict] = []
    for item in targets[:ASKS_PER_CONTRACT]:
        ctx = build_clause_context(text, ci, item.get("clause_ids") or [],
                                   primary_clause_id=item.get("primary_clause_id") or "")
        t0 = time.time()
        try:
            r = llm_ask.ask_about_item(
                question="这条条款的风险是什么？请引用原句说明。",
                item=item, contract_text=text, policies=[],
                clause_context=ctx, category=review_result.get("category") or "procurement",
            )
            dt = round(time.time() - t0, 1)
            ans = r.get("answer") or {}
            ev = r.get("evidence") or {}
            out.append({
                "file": fname,
                "item_id": item.get("id"),
                "seconds": dt,
                "ok": bool(r.get("ok")),
                "parse_ok": bool(ans) and bool(str(ans.get("问题是啥") or "").strip()),
                "fields_filled": sum(1 for f in ("风险等级", "这条在查啥", "问题是啥", "建议怎么改")
                                     if str(ans.get(f) or "").strip()),
                "quote_verified": bool(r.get("quote_verified")),
                "evidence_verification": ev.get("verification"),
                "honest_decline": (str(ans.get("原文在哪") or "") == "未定位到原文"),
            })
        except Exception as exc:  # noqa: BLE001
            out.append({"file": fname, "item_id": item.get("id"),
                        "ok": False, "error": type(exc).__name__})
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", required=True, choices=("glm", "deepseek"))
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    setup_profile(args.profile)

    results: dict = {"profile": args.profile, "contracts": [], "asks": []}
    for fname, category in CONTRACTS:
        review, full = run_contract(fname, category)
        results["contracts"].append(review)
        print(f"[{args.profile}] {fname}: {review['status']} in {review['seconds']}s | "
              f"需关注 {review['attention_items']}/{review['items_total']} | "
              f"quality={review['quality_available']} objections={review['objections_available']}",
              flush=True)
        results["asks"].extend(run_asks(full, fname))

    # 汇总
    asks = results["asks"]
    ok_asks = [a for a in asks if a.get("ok")]
    results["summary"] = {
        "ask_total": len(asks),
        "ask_ok": len(ok_asks),
        "ask_parse_ok": sum(1 for a in ok_asks if a.get("parse_ok")),
        "ask_quote_verified": sum(1 for a in ok_asks if a.get("quote_verified")),
        "ask_avg_seconds": (round(sum(a.get("seconds") or 0 for a in ok_asks) / len(ok_asks), 1)
                            if ok_asks else None),
        "review_total_seconds": round(sum(c["seconds"] for c in results["contracts"]), 1),
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    io.open(out, "w", encoding="utf-8").write(json.dumps(results, ensure_ascii=False, indent=2))
    print(f"[{args.profile}] summary: {json.dumps(results['summary'], ensure_ascii=False)}")
    print(f"saved -> {out}")


if __name__ == "__main__":
    main()
