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


def setup_profile(profile: str, model: str = "") -> None:
    """glm = easyrouter + glm-5.3-flash（走 ZHIPU_* 通道）；
    deepseek = 官方 API（key 从部署凭据文件读，不进对话）。"""
    # P1（Codex）：清光所有供应商端点/分层模型覆盖——环境残留的
    # DEEPSEEK_API_BASE / {PROVIDER}_MODEL_{PURPOSE} 会让对比静默走偏
    for k in (
        "ZHIPU_API_KEY", "ZHIPU_API_BASE", "GLM_MODEL", "GLM_API_KEY",
        "DEEPSEEK_API_KEY", "DEEPSEEK_MODEL", "DEEPSEEK_API_BASE",
        "GROK_MODEL", "XAI_API_KEY",
        "ZHIPU_MODEL_PRECHECK", "ZHIPU_MODEL_REVIEW",
        "GLM_MODEL_PRECHECK", "GLM_MODEL_REVIEW",
        "DEEPSEEK_MODEL_PRECHECK", "DEEPSEEK_MODEL_REVIEW",
        "GROK_MODEL_PRECHECK", "GROK_MODEL_REVIEW",
    ):
        os.environ.pop(k, None)
    if profile == "glm":
        env = _load_env_file(ROOT / ".env")
        for k in ("ZHIPU_API_BASE", "ZHIPU_API_KEY", "GLM_MODEL"):
            if k in env and env[k]:
                os.environ[k] = env[k]
        if not os.environ.get("ZHIPU_API_KEY"):
            print("FATAL: .env 无 ZHIPU_API_KEY", file=sys.stderr)
            sys.exit(2)
        if model:
            os.environ["GLM_MODEL"] = model  # 同通道换模型零改码
    elif profile == "deepseek":
        # P2（Codex）：调用方已设 DEEPSEEK_API_KEY 则直接用（可移植）；
        # 否则从凭据文件读，路径可用 MODEL_AB_CREDENTIALS_FILE 覆盖
        caller_key = os.environ.get("MODEL_AB_DEEPSEEK_KEY", "")
        creds_path = Path(os.environ.get("MODEL_AB_CREDENTIALS_FILE", "")
                          or Path(r"F:\合同审查Agent\.deploy-credentials.txt"))
        creds = _load_env_file(creds_path)
        key = caller_key or creds.get("deepseek_key", "")
        if not key:
            print(f"FATAL: 无 DeepSeek key（环境 MODEL_AB_DEEPSEEK_KEY / {creds_path} 均未提供）",
                  file=sys.stderr)
            sys.exit(2)
        os.environ["DEEPSEEK_API_KEY"] = key
        # P2（Codex）：_model_for 只在变量缺失时回落默认——空串会把请求
        # 变成空模型名。凭据未给模型就保持未设，走 DEFAULT_DEEPSEEK_MODEL
        if (creds.get("deepseek_model") or "").strip():
            os.environ["DEEPSEEK_MODEL"] = creds["deepseek_model"].strip()
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
            # P2（Codex）：响应里的「原文在哪」已被核验器覆写成占位文案——
            # 诚实度必须从核验**前**的原始模型输出（raw_text）测
            model_quote, honest_decline = None, None
            try:
                original = json.loads(r.get("raw_text") or "{}")
                model_quote = str(original.get("原文在哪") or "").strip()
                honest_decline = ("未定位" in model_quote) or (not model_quote)
            except (json.JSONDecodeError, AttributeError):
                pass
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
                "model_attempted_quote": bool(model_quote) and not (
                    honest_decline if honest_decline is not None else False),
                "honest_decline": honest_decline,
            })
        except Exception as exc:  # noqa: BLE001
            out.append({"file": fname, "item_id": item.get("id"),
                        "ok": False, "error": type(exc).__name__})
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", required=True, choices=("glm", "deepseek"))
    ap.add_argument("--out", required=True)
    ap.add_argument("--model", default="", help="覆盖 GLM_MODEL（同通道换模型）")
    args = ap.parse_args()
    setup_profile(args.profile, args.model)

    results: dict = {"profile": args.profile, "model": os.environ.get("GLM_MODEL", ""),
                     "contracts": [], "asks": []}
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
