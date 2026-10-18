# -*- coding: utf-8 -*-
"""B2-3 批2 双模型影子校准（设计稿 docs/spec-b23-classification-gold.md v1.2）。

红线（硬接口，spec-b2 §3.1）：
- 模型只给建议品类，永不切换；所有不一致一律 category_confirm（施工实现，
  本脚本只产校准数据）。
- 留痕八字段：provider/确切模型版本/提示词哈希/输入覆盖范围/延迟/费用/
  原始结构化输出/降级原因。
- 语料以冻结划分（fixtures-classification/MANIFEST.json + MANIFEST
  support_splits）为准；留出集仅终评（--split holdout），阈值只看校准集。

用法：
  python -X utf8 tools/b23/shadow_calibration.py --provider glm --split calibration
  python -X utf8 tools/b23/shadow_calibration.py --provider jev --split calibration --limit 3
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

PROMPT = """你是合同品类分诊器。判断给定合同文本属于以下哪个品类，只输出 JSON：
{"category": "<八选一>", "confidence": <0-1 小数>}
品类定义：
- procurement: 买卖/采购/供货合同（货物交付与价款支付为主轴）
- lease: 租赁合同（设备/房屋/车辆等租赁）
- nda: 保密协议（NDA，保密义务为主轴）
- technical_service: 技术服务合同（受托方以技术知识为委托方解决特定技术问题）
- consulting: 咨询合同（提供咨询意见/成果建议，含技术咨询/工程咨询/管理咨询）
- labor_dispatch: 劳务派遣/人力资源外包合同（派遣单位/用工单位/劳动者三方）
- tech_transfer: 技术转让/技术许可合同（专利权/专利申请权/技术秘密/实施许可的权属移转或授权）
- other: 以上均不是
判定依据：合同标题、首部当事人称谓、核心义务结构。文本可能是空白模板——按栏目结构判断形态归属。"""

CATEGORIES = ["procurement", "lease", "nda", "technical_service",
              "consulting", "labor_dispatch", "tech_transfer", "other"]

JEV_INSTRUCTIONS = ("判断这份合同文本属于哪个品类。依据标题、当事人称谓与核心义务；"
                   "空白模板按栏目结构判断。")
JEV_CRITERIA = {
    "procurement": "买卖/采购/供货合同", "lease": "租赁合同", "nda": "保密协议",
    "technical_service": "技术服务合同（以技术知识解决特定技术问题）",
    "consulting": "咨询合同（含技术/工程/管理咨询）",
    "labor_dispatch": "劳务派遣/人力外包（三方结构）",
    "tech_transfer": "技术转让/许可（权属移转或授权）", "other": "以上均不是",
}


def prompt_hash_for(provider: str) -> str:
    """哈希对象=实际发送的提示词（Codex P2：Jev 不发 GLM PROMPT）。"""
    payload = PROMPT if provider == "glm" else json.dumps(
        {"instructions": JEV_INSTRUCTIONS, "criteria": JEV_CRITERIA}, ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()[:12]

# 法务标签（老钱签认 2026-10-16；含 2026-10-17 五格翻格→other——修订前
# 17/22 口径见 docs/b23/shadow_jev_calibration_20261017_080504.jsonl）+
# 冻结划分；support_splits 见仓库外 MANIFEST
REJECTION_LABELS = {
    "tech-consulting-shgov": "consulting",
    "management-consulting-zhujianbu": "consulting",
    "management-consulting-guangzhou": "consulting",
    "labor-dispatch-mohrss-official": "labor_dispatch",
    "labor-dispatch-shenzhen": "labor_dispatch",
    "labor-dispatch-shenyang": "labor_dispatch",
    "tech-transfer-patent-right-shgov": "tech_transfer",
    "tech-transfer-trade-secret-shgov": "tech_transfer",
    "tech-license-patent-shgov": "tech_transfer",
}
REJECTION_HOLDOUT = {"management-consulting-zhujianbu", "labor-dispatch-shenyang", "tech-license-patent-shgov"}
SUPPORT_HOLDOUT = {"school-uniform-procurement-guangzhou", "shanghai-residential-lease-2014",
                   "trade-secret-nda-guangdong", "tisvc-sjtu-2003"}

SUPPORT = [  # (样本名, 提取路径, 法务标签)
    ("cement-sale-gf-2008", "fixtures-real/cement-sale-gf-2008.docx", "procurement"),
    ("food-procurement-xinjiang-2025", "fixtures-real/food-procurement-xinjiang-2025.docx", "procurement"),
    ("gov-procurement-goods-mof-2024", "fixtures-real/gov-procurement-goods-mof-2024.docx", "procurement"),
    ("agri-produce-sale-samr-2025", "fixtures-real/agri-produce-sale-samr-2025.docx", "procurement"),
    ("school-uniform-procurement-guangzhou", "fixtures-real/school-uniform-procurement-guangzhou.docx", "procurement"),
    ("mandate-contract-samr-2025", "fixtures-real/mandate-contract-samr-2025.docx", "other"),
    ("construction-work-contract-2017", "fixtures-real/construction-work-contract-2017.docx", "other"),
    ("work-contract-gf-2000", "fixtures-real/work-contract-gf-2000.docx", "other"),
    ("raw-milk-purchase-2016", "fixtures-real/raw-milk-purchase-2016.docx", "procurement"),
    ("equipment-lease-construction-2000", "fixtures-real/equipment-lease-construction-2000.docx", "lease"),
    ("house-lease-gf-2025-2614", "fixtures-real/house-lease-gf-2025-2614.docx", "lease"),
    ("shanghai-residential-lease-2014", "fixtures-real/shanghai-residential-lease-2014.docx", "lease"),
    ("zhejiang-vehicle-lease-2024", "fixtures-real/zhejiang-vehicle-lease-2024.docx", "lease"),
    ("business-cooperation-nda-guangdong", "fixtures-real/business-cooperation-nda-guangdong.docx", "nda"),
    ("trade-secret-nda-guangdong", "fixtures-real/trade-secret-nda-guangdong.docx", "nda"),
    ("techdev-coop-most-2001", "fixtures-tisvc/techdev-coop-most-2001.txt", "other"),
    ("techdev-most-2001", "fixtures-tisvc/techdev-most-2001.txt", "other"),
    ("tisvc-most-2001", "fixtures-tisvc/tisvc-most-2001.txt", "technical_service"),
    ("tisvc-shgov-2020", "fixtures-tisvc/tisvc-shgov-2020.txt", "technical_service"),
    ("tisvc-sjtu-2003", "fixtures-tisvc/tisvc-sjtu-2003.txt", "technical_service"),
]


def load_text(path: str, max_chars: int = 6000) -> tuple[str, str]:
    """读样本文本；docx 走生产同构 python-docx。截断=头 max_chars+尾 2000（§5 截断策略）。"""
    p = ROOT / path
    if path.endswith(".docx"):
        from docx import Document  # 生产解析同构

        doc = Document(str(p))
        parts = [par.text for par in doc.paragraphs]
        for table in doc.tables:
            for row in table.rows:
                parts.append("\t".join(cell.text for cell in row.cells))
        text = "\n".join(parts)
    else:
        text = p.read_text(encoding="utf-8", errors="replace")
    if len(text) <= max_chars:
        return text, "full"
    tail = 2000
    marker = "\n...[中段截断]...\n"
    return text[:max_chars] + marker + text[-tail:], f"truncated_head_{max_chars}_tail_{tail}"


def _load_env() -> dict:
    env = {}
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def call_glm(text: str) -> tuple[dict, dict]:
    """官方智谱开放平台 glm-4-flash（.env: GLM4F_*，2026-10-17 临时测试 key）；
    coding 套餐中转（ZHIPU_*）不支持直接调用已排除。"""
    env = _load_env()
    base = env.get("GLM4F_API_BASE", "https://open.bigmodel.cn/api/paas/v4").rstrip("/")
    key, model = env["GLM4F_API_KEY"], env.get("GLM4F_MODEL", "glm-4-flash")
    body = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": PROMPT},
            {"role": "user", "content": f"合同文本：\n{text}"},
        ],
        "temperature": 0,
    }).encode()
    req = urllib.request.Request(f"{base}/chat/completions", data=body, headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=120) as r:
        resp = json.loads(r.read())
    latency = round(time.time() - t0, 2)
    usage = resp.get("usage", {})
    raw = resp["choices"][0]["message"]["content"]
    return parse_verdict(raw), {
        "provider": "glm", "model_version": model, "latency_s": latency,
        "cost_note": f"in={usage.get('prompt_tokens')} out={usage.get('completion_tokens')} tokens(中转计费未查单价)",
        "raw_output": raw, "degradation_reason": None,
    }


def call_jev(text: str) -> tuple[dict, dict]:
    """TypeSafe Jev choice 八类（预检已过，2026-10-16）。"""
    key = None
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith("TYPESAFE_API_KEY="):
            key = line.split("=", 1)[1].strip()
    body = json.dumps({"state": text, "model": "jev-latest", "questions": {
        "category": {"type": "choice",
            "instructions": JEV_INSTRUCTIONS, "criteria": JEV_CRITERIA}}}).encode()
    req = urllib.request.Request("https://api.typesafe.ai/v1/systemone", data=body, headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=120) as r:
        resp = json.loads(r.read())
    latency = round(time.time() - t0, 2)
    a = resp["answers"]["category"]
    usage = resp.get("usage", {})
    return {"category": a["choice"], "confidence": a.get("confidence")}, {
        "provider": "jev", "model_version": resp.get("model", "jev-latest"), "latency_s": latency,
        "cost_note": f"in={usage.get('input_tokens')} out={usage.get('output_tokens')} tokens(单价未定，上限核算见报告)",
        "raw_output": json.dumps(a, ensure_ascii=False), "degradation_reason": None,
    }


def call_clef(text: str) -> tuple[dict, dict]:
    """Cloudflare Clef（clef-flash）——独立 HTTP（校准工具不依赖 app 包）；
    生产路径走 app/services/llm_client.decision_judgment（账本单点）。"""
    env = _load_env()
    url = (f"{env.get('CF_AI_BASE', 'https://api.cloudflare.com/client/v4').rstrip('/')}"
           f"/accounts/{env['CF_ACCOUNT_ID']}/ai/run/@cf/cloudflare/{env.get('CF_CLEF_MODEL', 'clef-flash')}")
    body = json.dumps({"model": env.get("CF_CLEF_MODEL", "clef-flash"),
                       "state": text, "questions": {"category": {
                           "type": "choice",
                           "instructions": JEV_INSTRUCTIONS,
                           "criteria": dict(JEV_CRITERIA)}}}).encode()
    req = urllib.request.Request(url, data=body, headers={
        "Authorization": f"Bearer {env['CF_API_TOKEN']}",
        "Content-Type": "application/json"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read())
    except Exception as e:
        return {"category": None, "confidence": None}, {
            "provider": "clef", "model_version": env.get("CF_CLEF_MODEL", "clef-flash"),
            "latency_s": round(time.time() - t0, 2), "cost_note": None,
            "raw_output": str(e)[:300],
            "degradation_reason": f"llm_error: {type(e).__name__}"}
    a = (data.get("answers") or data.get("result", {}).get("answers") or {}).get("category", {})
    usage = data.get("usage") or data.get("result", {}).get("usage") or {}
    return {"category": a.get("choice"), "confidence": a.get("confidence")}, {
        "provider": "clef", "model_version": env.get("CF_CLEF_MODEL", "clef-flash"),
        "latency_s": round(time.time() - t0, 2),
        "cost_note": f"in={usage.get('input_tokens')} out={usage.get('output_tokens')} tokens(免费额度)",
        "raw_output": json.dumps(a, ensure_ascii=False)[:500],
        "degradation_reason": None}


def _load_env() -> dict:
    env = {}
    from pathlib import Path
    dot_env = Path(__file__).resolve().parents[2] / ".env"
    if dot_env.exists():
        for line in dot_env.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    import os
    env.update({k: v for k, v in os.environ.items() if k.startswith(("CF_", "TYPESAFE_", "GLM4F_"))})
    return env


def parse_verdict(raw: str) -> dict:
    """GLM JSON 解析+Schema 校验；失败重试由调用方按降级留痕处理。"""
    m = re.search(r"\{[^{}]*\}", raw, re.S)
    if not m:
        raise ValueError(f"no json in output: {raw[:120]}")
    data = json.loads(m.group(0))
    if data.get("category") not in CATEGORIES:
        raise ValueError(f"bad category: {data.get('category')}")
    conf = float(data.get("confidence", 0))
    return {"category": data["category"], "confidence": max(0.0, min(1.0, conf))}


def model_of(provider: str) -> str:
    if provider == "jev":
        return "jev-latest"
    if provider == "clef":
        import os
        return os.environ.get("CF_CLEF_MODEL", "clef-flash")
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith("GLM4F_MODEL="):
            return line.split("=", 1)[1].strip()
    return "glm-4-flash"


# ---------- 预算闸（用户复核 P1：$5 硬上限必须进执行路径，不只进说明书） ----------

# 固定计费表（可审计）。Jev 单价来源：TypeSafe 控制台实测（用户 2026-10-17 提供：
# 269,257 tokens 实扣 $0.0111 ≈ $0.041/M tokens），上取整留 20% 余量；
# 单价变动须同步改表+重审。GLM 官方免费档计 0（临时测试 key，商用定价待定）。
PRICE_TABLE = {  # USD per 1M tokens
    "jev": 0.05,
    "clef": 0.05,
    "glm": 0.0,
}
EST_TOKENS_PER_CHAR = 1.0  # 中文保守上界（实际 ~0.6-0.75），宁高估不低估


def estimate_request_cost_usd(provider: str, text: str) -> float:
    """单请求成本上界（可审计：纯函数，单价来自 PRICE_TABLE）。"""
    est_tokens = len(text) * EST_TOKENS_PER_CHAR
    return est_tokens / 1_000_000 * PRICE_TABLE[provider]


def check_budget(remaining_usd: float, next_cost_usd: float) -> bool:
    """发请求前的唯一闸门：下一笔预估≤剩余额度才放行。"""
    return next_cost_usd <= remaining_usd


def run_calibration(samples: list[tuple[str, str, str]], caller, provider: str,
                    prompt_hash: str, budget_usd: float, writer) -> tuple[int, int, float, int]:
    """跑批（可注入 caller/writer 供守卫测试）：首次超限立即停止整批
    （防半截批次污染校准记录——跳过后续样本，哪怕它们更小装得下），返回
    (correct, total, spent_usd, skipped_by_budget)。"""
    correct = total = skipped = 0
    spent = 0.0
    for name, path, label in samples:
        text, coverage = load_text(path)
        est = estimate_request_cost_usd(provider, text)
        if not check_budget(budget_usd - spent, est):
            skipped = len(samples) - total  # 本样本+其后全部未跑
            print(f"[BUDGET] {name} 预估 ${est:.4f} > 剩余 ${budget_usd - spent:.4f}"
                  f"——超限即停批（本条及其后 {skipped} 条全部不发）")
            break
        spent += est
        verdict, meta = None, None
        t_sample = time.time()
        try:
            verdict, meta = caller(text)
        except Exception as e:  # 降级留痕（不中断批次；保留请求模型与耗时）
            t_err = round(time.time() - t_sample, 2)
            meta = {"provider": provider,
                    "model_version": model_of(provider),
                    "latency_s": t_err,
                    "cost_note": None, "raw_output": str(e)[:300],
                    "degradation_reason": f"parse_or_http_error: {type(e).__name__}"}
            verdict = {"category": None, "confidence": None}
        total += 1
        ok = verdict["category"] == label
        correct += ok
        rec = {"sample": name, "path": path, "gold_label": label,
               "input_coverage": coverage, "prompt_hash": prompt_hash,
               "est_cost_usd": round(est, 4), "verdict": verdict, "correct": ok, **meta}
        writer(json.dumps(rec, ensure_ascii=False) + "\n")
        mark = "OK " if ok else "MISS"
        print(f"[{total}] {mark} {name} gold={label} pred={verdict['category']} "
              f"conf={verdict['confidence']}")
    return correct, total, spent, skipped


def preflight_budget_total(samples: list[tuple[str, str, str]], provider: str) -> float:
    """整批总成本上界（逐份 load_text 估算之和，含截断后的实际输入）。"""
    return sum(estimate_request_cost_usd(provider, load_text(path)[0])
               for _, path, _ in samples)


def run_batch_with_preflight(samples: list[tuple[str, str, str]], caller, provider: str,
                             prompt_hash: str, budget_usd: float,
                             out_path: Path) -> tuple[int, int, int, float, int]:
    """预检→跑批→落盘（复核 P1：预算须在首个请求前预检全批——超限在任何
    caller 与文件创建前退出；批中再遇超限为防御兜底，同样零半截文件）。"""
    total_est = preflight_budget_total(samples, provider)
    if not check_budget(budget_usd, total_est):
        print(f"[BUDGET] 整批总上界 ${total_est:.4f} > 预算 ${budget_usd:.2f}"
              f"——预检不过，零请求零文件退出（退出码 2）")
        return 2, 0, 0, 0.0, len(samples)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    correct, total, spent, skipped = run_calibration(
        samples, caller, provider, prompt_hash, budget_usd, lines.append)
    with open(out_path, "w", encoding="utf-8") as f:
        f.writelines(lines)
    print(f"\n{provider}: {correct}/{total} correct, "
          f"budget_spent≈${spent:.4f}/${budget_usd:.2f}, "
          f"skipped_by_budget={skipped} -> {out_path}")
    if skipped:
        print("预算超限截停：本批未跑完（退出码 2）")
        return 2, correct, total, spent, skipped
    return 0, correct, total, spent, skipped


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", choices=["glm", "jev", "clef"], required=True)
    ap.add_argument("--split", choices=["calibration", "holdout"], default="calibration")
    ap.add_argument("--limit", type=int, default=0, help="试射限量（0=全量）")
    ap.add_argument("--budget-usd", type=float, default=5.0,
                    help="本批预算硬上限 USD（默认 5.0；超限即停，退出码 2）")
    args = ap.parse_args()

    caller = {"glm": call_glm, "jev": call_jev, "clef": call_clef}[args.provider]
    prompt_hash = prompt_hash_for(args.provider)

    samples = []
    for name, path, label in SUPPORT:
        if name not in SUPPORT_HOLDOUT:
            samples.append((name, path, label))
    for name, label in REJECTION_LABELS.items():
        if name not in REJECTION_HOLDOUT:
            samples.append((name, f"fixtures-classification/{name}.txt", label))
    if args.split == "holdout":
        hold = [(n, p, lb) for n, p, lb in SUPPORT if n in SUPPORT_HOLDOUT]
        hold += [(n, f"fixtures-classification/{n}.txt", lb)
                 for n, lb in REJECTION_LABELS.items() if n in REJECTION_HOLDOUT]
        samples = hold
    if args.limit:
        samples = samples[: args.limit]

    out_dir = ROOT / "docs" / "b23"
    out_path = out_dir / f"shadow_{args.provider}_{args.split}_{time.strftime('%Y%m%d_%H%M%S')}.jsonl"
    code, _correct, _total, _spent, _skipped = run_batch_with_preflight(
        samples, caller, args.provider, prompt_hash, args.budget_usd, out_path)
    return code


if __name__ == "__main__":
    sys.exit(main())
