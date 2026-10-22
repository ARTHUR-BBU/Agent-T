# -*- coding: utf-8 -*-
"""M6.5 收官回测（老钱裁定残-2/残-3，2026-10-22）：一次数据出两份结论。

纪律（docs/m65/residual-ruling-20261022.md）：
- 17 份全集 + 2 份表格靶向卷同批同哈希（本脚本启动时记录 git 哈希，禁跨版本拼接）；
- 两份结论分开出：①表格专项闭环结论（残-2 判据）②老体例评估结论（v2.1 第 7 条判据），
  判据不同不得混写；
- 证据链按证物实际位置分列：food 验表格路径（第几行第几格），work 验正文段落
  （A1 验尸 P06/P13）；只给 pass/未命中结论的记录不验收；
- 不中先二次验尸再分流（抽取文本可读到而未命中=词表缺口挂 A3；读不到=抽取缺口
  另立小批），验尸结论打印入记录；
- 漏报对照必须区分「体例特异性漏报」与「通用词表缺口漏报」，老体例启动判据只计前者；
- 引用位置判定一律以抽取层原始文本为准，不以 Ask 层引用为准（残-4 提醒）。

用法（需生产在跑、成本按 M6.5 体积分级规矩；必须在仓库根目录以模块方式跑）：
    python -X utf8 -m tools.m65.final_replay --out docs/m65/final-replay.json
实际 LLM 跑批委托 tools/m65/run_eval.py（同一 --base/--auth 通道），本脚本负责
靶向证据链与**原始证据产物**——两份结论由人工按 docs/m65/final-replay-record-template.md
填写，本脚本不自动出结论（Codex P2）。运行前须用户批准成本。

名册纪律（Codex P1）：全集卷名从 run1.json+run2.json 两轮留档机器取并集，
校验恰为 17 卷；源留档哈希与最终名册写入产物。留档名册变动→集数漂移→直接报错。
版本纪律（Codex 勘正）：调用方 HEAD 与**服务端部署版本**（--server-host 的容器
镜像标签）都记录；不一致直接报错——「同批同哈希」指的是服务端实际跑的版本。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from typing import Any
import pathlib

# 直跑引导（Codex 勘正：python tools/m65/final_replay.py 直跑时 sys.path 是
# tools/m65，摸不到仓库根的 app 包——与既有可直接执行工具同款引导）
if __package__ in (None, ""):
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
import subprocess
import time

# 名册来源（两轮实测留档；卷名一律机器取数，禁手写清单——Codex P1）
ARCHIVES = ("docs/m65/run1.json", "docs/m65/run2.json")
EXPECTED_ROSTER_SIZE = 17


def load_roster(repo_root: pathlib.Path) -> tuple[list[str], dict[str, str]]:
    """从两轮留档机取名册并校验；返回 (排序名册, 源留档哈希表)。

    集合漂移（并集≠17）直接 SystemExit——防留档变动后脚本悄悄跑旧卷。
    """
    roster: set[str] = set()
    source_hashes: dict[str, str] = {}
    for rel in ARCHIVES:
        path = repo_root / rel
        if not path.exists():
            raise SystemExit(f"FAIL: 名册来源缺失 {rel}")
        data = path.read_bytes()
        source_hashes[rel] = hashlib.sha256(data).hexdigest()[:16]
        files = json.loads(data).get("files") or []
        if not files:
            raise SystemExit(f"FAIL: {rel} 无 files 名册（留档格式变动？）")
        roster.update(files)
    if len(roster) != EXPECTED_ROSTER_SIZE:
        raise SystemExit(
            f"FAIL: 名册集数漂移——并集 {len(roster)} 卷 ≠ 预期 {EXPECTED_ROSTER_SIZE}"
            f"（来源：{ARCHIVES}）。留档变动须经老钱/外审确认后再改预期值。")
    return sorted(roster), source_hashes

# 表格靶向证据链目标（残-2 勘正版：按证物实际位置分列）
TARGETS = {
    "food-procurement-xinjiang-2025.docx": {
        "term": "材料名称", "loc": "table", "item": "subject_matter",
        "why": "v2.1 第 6 条金标证物：标的信号仅存在于表格文本",
    },
    "work-contract-gf-2000.docx": {
        "term": "承揽项目", "loc": "paragraph", "item": "subject_matter",
        "why": "A1 验尸 P06/P13 正文段（不在表格——证据链按段落位置记录）",
    },
}


def git_hash() -> str:
    return subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True,
                          text=True, check=True).stdout.strip()


def _docx_tables(path: pathlib.Path) -> list[tuple[int, int, int, str]]:
    """返回 (表序, 行, 列, 文本)——food 表格路径证据链用。"""
    import docx
    d = docx.Document(str(path))
    out = []
    for ti, table in enumerate(d.tables, 1):
        for ri, row in enumerate(table.rows, 1):
            for ci, cell in enumerate(row.cells, 1):
                out.append((ti, ri, ci, cell.text))
    return out


def evidence_chain(fixtures: pathlib.Path) -> list[dict]:
    """靶向证据链：证物词在抽取文本中的实际位置 + 引擎 pass 判定。"""
    from app.services.extract import extract_text
    records = []
    for fname, spec in TARGETS.items():
        path = fixtures / fname
        text = extract_text(fname, path.read_bytes())
        rec = {"paper": fname, "term": spec["term"], "expected_loc": spec["loc"],
               "why": spec["why"], "autopsy_verdict": None, "engine": None}
        # 二次验尸第一步：抽取文本里到底有没有该词（打印入记录）
        idx = text.find(spec["term"])
        rec["in_extracted_text"] = idx >= 0
        if idx >= 0:
            rec["context"] = text[max(0, idx - 40): idx + 60].replace("\n", "⏎")
        if spec["loc"] == "table":
            hits = [(t, r, c) for t, r, c, txt in _docx_tables(path)
                    if spec["term"] in txt]
            rec["table_position"] = hits[0] if hits else None
            rec["autopsy_verdict"] = ("表格内实锤" if hits else
                                      "表格与正文均未读到——抽取缺口嫌疑")
        else:
            # 段落命中带编号，基准=抽取文本非空行号（残-4：以抽取层原始文本为准）。
            # 勘正注：A1 验尸的 P06/P13 编号基准不可复现（现行抽取层为行 7/16）；
            # 回测记录以「抽取文本行号+原文摘录」为准，内容比对证明同一证物。
            lines = text.splitlines()
            paras = [(i, ln.strip()) for i, ln in enumerate(lines, 1)
                     if spec["term"] in ln.strip()]
            rec["numbering_basis"] = "抽取文本非空行号（1 基）"
            rec["a1_autopsy_note"] = "A1 验尸记 P06/P13，编号基准与现行抽取层不同；以原文内容比对为准"
            rec["paragraph_hits"] = [{"line": i, "text": t} for i, t in paras]
            rec["autopsy_verdict"] = (f"正文实锤（{len(paras)} 处：行 "
                                      f"{'/'.join(str(i) for i, _ in paras)}）"
                                      if paras
                                      else "正文与表格均未读到——抽取缺口嫌疑")
        records.append(rec)
    return records


_HEX = set("0123456789abcdefABCDEF")


def check_revision(caller_head: str, server_rev: str) -> tuple[bool, str]:
    """版本比对（Codex P1 最小标准）：调用方恒存 40 位全 SHA 归档；服务端
    标签只接受「合法 Git SHA 且为调用方全 SHA 的前缀」（同提交短标签放行、
    不同 SHA 拒绝、非 SHA 标签拒绝）。返回 (是否一致, 不一致原因)。"""
    tag = server_rev.strip()
    if not (7 <= len(tag) <= 40) or any(c not in _HEX for c in tag):
        return False, f"服务端标签不是合法 Git SHA：{tag!r}"
    if not caller_head.lower().startswith(tag.lower()):
        return False, f"服务端 {tag} 不是调用方 {caller_head} 的前缀（不同提交）"
    return True, ""


def server_revision(host: str, container: str = "agent-t") -> str:
    """读服务端实际部署版本（容器镜像标签）——「同批同哈希」以它为准。"""
    r = subprocess.run(
        ["ssh", host,
         f"docker inspect {container} --format '{{{{.Config.Image}}}}'"],
        capture_output=True, text=True, check=True)
    image = r.stdout.strip()
    return image.split(":")[-1] if ":" in image else image


# ---- 费用上限单 v2（Codex P1/P2 勘正后机算重写；原 ¥4/¥6、¥30 上限作废）----
# 牌价依据（官方页 api-docs.deepseek.com/quick_start/pricing，2026-10-22 抓取原文）：
#   现售模型=deepseek-flash / deepseek-v4-pro（生产模型名 deepseek-chat 为 legacy，
#   页面未标其映射价——故按**页上最贵的 v4-pro 峰值**计：入 $1.32/M、出 $3.96/M，
#   汇率 7.3）。生产 DEEPSEEK_MODEL 实际版本以 env 为准，回测记录如实登记。
# 最坏情况界（机算 2026-10-22，17 卷真实抽取字数 186,134 字）：
#   1 token/字（中文实际约 0.6-0.7，取 1 冗余）× 每卷 20 次调用（预算 18+余量）
#   × 每次带 3k prompt 开销 + 输出每次 ≤2k → 入 4.74M + 出 0.68M ≈ **¥65.36**
# 余额 ¥42.80 不足最坏情况界 → **跑前必须充值 ≥ ¥66**，否则拒绝开跑。
USD_CNY = 7.3
PRICE_IN_CNY_PER_M = 1.32 * USD_CNY    # ¥9.636/M（v4-pro 峰值 cache miss）
PRICE_OUT_CNY_PER_M = 3.96 * USD_CNY   # ¥28.908/M（峰值）
MAX_COST_CNY = 66.0                    # = 最坏情况界（机算；改动须走 PR）
ALERT_COST_CNY = 33.0                  # 50%


def estimate_cost(in_tokens: float, out_tokens: float) -> float:
    """按冗余牌价估算人民币成本。"""
    return in_tokens / 1e6 * PRICE_IN_CNY_PER_M + out_tokens / 1e6 * PRICE_OUT_CNY_PER_M


def deepseek_balance(api_key: str) -> float:
    """查 DeepSeek 账户 CNY 余额（key 从 auth 文件读，不落命令行/日志）。"""
    import urllib.request
    req = urllib.request.Request("https://api.deepseek.com/user/balance",
                                 headers={"Authorization": f"Bearer {api_key}"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read())
    for info in data.get("balance_infos", []):
        if info.get("currency") == "CNY":
            return float(info["total_balance"])
    raise SystemExit("FAIL: 余额接口无 CNY 条目")


def spent_since(server_host: str, container: str, since_ts: float) -> tuple[int, int]:
    """查生产账本本批累计 tokens（llm_calls.created_at ≥ since_ts）。

    生产账本在服务器容器内——经 ssh 读（与部署工具同通道）。远端统计脚本
    经 stdin 送入（不经命令行，免嵌套引号地狱）。
    """
    remote_code = (
        "import sqlite3,json,sys\n"
        "con = sqlite3.connect('/tmp/agent-t-reviews.db')\n"
        "start = float(sys.argv[1])\n"
        "rows = con.execute('SELECT usage FROM llm_calls WHERE created_at >= ?',"
        " (start,)).fetchall()\n"
        "ins = sum(json.loads(u[0]).get('input_tokens') or 0 for u in rows)\n"
        "outs = sum(json.loads(u[0]).get('output_tokens') or 0 for u in rows)\n"
        "print(json.dumps([ins, outs]))\n"
    )
    r = subprocess.run(
        ["ssh", server_host,
         f"docker exec -i {container} python3 - {since_ts:.3f}"],
        input=remote_code, capture_output=True, text=True, check=True)
    i, o = json.loads(r.stdout.strip().splitlines()[-1])
    return int(i), int(o)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fixtures", default="fixtures-real")
    ap.add_argument("--out", default="docs/m65/final-replay.json")
    ap.add_argument("--base", default="http://59.110.13.13:8080")
    ap.add_argument("--insecure", action="store_true")
    ap.add_argument("--skip-llm", action="store_true",
                    help="只出靶向证据链与骨架（不跑 LLM 全集，供模板预览）")
    ap.add_argument("--server-host", default="root@59.110.13.13",
                    help="生产主机（ssh 可达），用于读容器镜像标签=部署版本")
    # 费用硬上限=模块常量 MAX_COST_CNY（Codex P1：可改大的上限不是硬上限；
    # 改动须走 PR 审计），不设 CLI 旗标
    ap.add_argument("--auth-file", default=r"F:\合同审查Agent\.deploy-credentials.txt")
    args = ap.parse_args()

    repo_root = pathlib.Path.cwd()
    roster, roster_sources = load_roster(repo_root)
    fixtures = repo_root / args.fixtures
    missing = [f for f in roster if not (fixtures / f).exists()]
    if missing:
        raise SystemExit(f"FAIL: 全集缺卷 {missing}")

    record: dict[str, Any] = {
        "purpose": "M6.5 收官回测（残-2 表格专项闭环 + 残-3 老体例评估，两份结论分开）",
        "git_hash": git_hash(),
        "started_at": time.strftime("%Y-%m-%d %H:%M"),
        "freeze_note": "单批锁单一 git 哈希（裁定残-3 执行条件 1）",
        # Codex P1：名册机器取数+来源哈希入产物（集数漂移已在 load_roster 报错）
        "roster_source_hashes": roster_sources,
        "roster": roster,
    }
    # Codex 勘正：记录服务端部署版本；与调用方 HEAD 不一致直接报错
    #（--skip-llm 干跑纯本地零服务端调用，只记录不拦——版本纪律管的是 LLM 批）
    server_rev = server_revision(args.server_host)
    record["caller_head"] = record.pop("git_hash")
    record["server_revision"] = server_rev
    rev_ok, rev_reason = check_revision(record["caller_head"], server_rev)
    record["revision_verdict"] = "一致" if rev_ok else f"不一致（{rev_reason}）"
    if not rev_ok:
        if args.skip_llm:
            print(f"[干跑] 版本不一致备案：{rev_reason}（不影响本地证据链；"
                  f"真跑批前须对齐）")
        else:
            raise SystemExit(f"FAIL: 版本不一致——{rev_reason}。"
                             f"先部署对齐再跑「同批同哈希」。")
    print("冻结哈希（调用方）:", record["caller_head"][:12],
          "| 服务端部署:", server_rev, "| 名册:", len(roster), "卷")

    # ① 靶向证据链（本地抽取层，零 LLM 成本）
    record["evidence_chain"] = evidence_chain(fixtures)
    for r in record["evidence_chain"]:
        print(f"[靶向] {r['paper']} 「{r['term']}」: {r['autopsy_verdict']}"
              f" 位置={r.get('table_position') or r.get('paragraph_hits')}")

    # ② LLM 全集跑批（委托既有跑道；费用上限单见 record["cost_memo"]）
    if not args.skip_llm:
        # 费用预检（Codex 要求：发请求前报数字+余额兜底核验）
        cred = {}
        for line in pathlib.Path(args.auth_file).read_text(encoding="utf-8").splitlines():
            if "=" in line:
                k, _, v = line.partition("=")
                cred[k.strip()] = v.strip()
        balance = deepseek_balance(cred["deepseek_key"])
        record["cost_memo"] = {
            "牌价依据": "官方页最贵 v4-pro 峰值：入 $1.32/M、出 $3.96/M × 汇率 7.3"
                        "（生产模型名 deepseek-chat 为 legacy，页未标映射价——按最贵计）",
            "最坏情况界": "¥65.36（17 卷真实 186,134 字 × 1 token/字 × 20 调用"
                          " × 3k prompt 开销 + 出 ≤2k/次，机算可复现）",
            "硬上限人民币": MAX_COST_CNY,
            "告警线人民币": ALERT_COST_CNY,
            "超限方式": "跑前余额≥硬上限否则拒绝；跑中每 60s 按生产账本累计 usage"
                        " 实时核算，≥告警线打印、≥硬上限终止子进程；末次结算必做，"
                        "实际花费超界→记录标记「结论作废待人工复核」",
            "deepseek_balance_cny": balance,
        }
        if balance < MAX_COST_CNY:
            raise SystemExit(
                f"FAIL: 余额 ¥{balance} < 最坏情况界 ¥{MAX_COST_CNY}"
                f"——先充值 ≥ ¥{MAX_COST_CNY - balance:.2f} 再跑（费用上限单见记录）")
        print(f"[费用上限单] 硬上限(最坏情况界) ¥{MAX_COST_CNY} / 告警 ¥{ALERT_COST_CNY}"
              f" / 余额 ¥{balance}")
        names = ",".join(roster)
        run_out = str(pathlib.Path(args.out).parent / "final-replay-run.json")
        cmd = [sys_exec(), "-X", "utf8", "tools/m65/run_eval.py",
               "--fixtures", str(fixtures), "--names", names,
               "--out", run_out, "--base", args.base]
        if args.insecure:
            cmd.append("--insecure")
        print("全集跑批（约 40-60 分钟，DeepSeek 计费）：", " ".join(cmd[2:]))
        start_ts = time.time()
        proc = subprocess.Popen(cmd)
        while proc.poll() is None:
            time.sleep(60)
            if proc.poll() is not None:
                break
            try:
                i_tok, o_tok = spent_since(args.server_host, "agent-t", start_ts)
                cost = estimate_cost(i_tok, o_tok)
                print(f"[费用哨兵] 已用 入{i_tok}/出{o_tok} tokens ≈ ¥{cost:.2f}")
                if cost >= MAX_COST_CNY:
                    proc.kill()
                    raise SystemExit(f"FAIL: 费用 ¥{cost:.2f} ≥ 硬上限 "
                                     f"¥{MAX_COST_CNY}——已终止跑批")
                if cost >= ALERT_COST_CNY:
                    print(f"[费用告警] ¥{cost:.2f} ≥ 告警线 ¥{ALERT_COST_CNY}")
            except SystemExit:
                raise
            except Exception as exc:  # 哨兵自身故障不误杀跑批，打印继续
                print("[费用哨兵] 查询失败（不终止）：", type(exc).__name__)
        if proc.returncode != 0:
            raise SystemExit(f"FAIL: run_eval 退出码 {proc.returncode}")
        # 末次结算必做（Codex P1：最后一轮检查后的花费不流失）
        i_fin, o_fin = spent_since(args.server_host, "agent-t", start_ts)
        final_cost = estimate_cost(i_fin, o_fin)
        record["final_settlement"] = {
            "input_tokens": i_fin, "output_tokens": o_fin,
            "cost_cny": round(final_cost, 2),
            "verdict": ("在界内" if final_cost <= MAX_COST_CNY
                        else "超界——本批结论作废，待人工复核"),
        }
        print(f"[末次结算] ¥{final_cost:.2f} → {record['final_settlement']['verdict']}")
        record["run_eval_output"] = run_out
    else:
        record["run_eval_output"] = None
        print("[skip-llm] 仅出证据链与骨架")

    # ③ 原始证据产物到此为止——两份结论由人工按 final-replay-record-template.md
    # 填写（Codex P2：本脚本不自动出结论）。以下仅预置判据与待填字段。
    record["conclusion_1_table_closure"] = {
        "判据": "残-2：food 表格路径命中+work 正文位置命中，证据链齐备",
        "结果": "待跑批后填充",
    }
    record["conclusion_2_legacy_eval"] = {
        "判据": "v2.1 第 7 条：体例特异性漏报集中 且 claim_id 率<0.3（只计体例特异，"
                "通用词表缺口归 A3 不计）",
        "结果": "待跑批后填充",
        "漏报对照表": "体例特异性漏报 / 通用词表缺口漏报 两列分记（残-3 执行条件 4）",
    }
    out = pathlib.Path(args.out)
    out.write_text(json.dumps(record, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    print("骨架/记录：", out)


def sys_exec() -> str:
    import sys
    return sys.executable


if __name__ == "__main__":
    main()
