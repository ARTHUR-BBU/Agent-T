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


# ---- 费用上限单 v4（2026-10-23 codex 口径：按 #137 真水龙头 caps 算最坏输出）----
# （v3 的 flash 牌价不变；输出最坏情况从「估算 0.68M」改为「caps 全开 4.59M」）----
# 牌价依据（api-docs.deepseek.com/zh-cn/quick_start/pricing，2026-10-22 抓取原文）：
#   生产 env DEEPSEEK_MODEL=deepseek-v4-flash；官方注(1)：旧模型名由
#   DeepSeek-V4.1-Flash-1017 服务、**按 Flash 价格计费**。
#   Flash 牌价（元/百万 tokens）：输入（缓存未命中）高峰 2 / 空闲 1；输出高峰 8 / 空闲 4。
#   （v4-pro 列 9/4.5、27/13.5——生产不用该列。）
# 峰谷（官方注(2)）：高峰=北京时间周一至五 9-12、14-18；其余（含周末节假日）=空闲半价。
# 最坏情况界 v4.1（2026-10-23，codex 勘正版：逐节点公式读 #137 阀门常量）：
#   输出/卷 = precheck 2×3k + model_review 6×13k + quality 6×25k
#             + objection 2×10k + ask 2×8k = **270k**（函数实时读阀门表算出）
#   × 17 卷 = 4.59M × ¥8/M（Flash 峰值输出）= **¥36.72**
#   输入 = 17 卷 186,134 字 × 1 token/字（冗余）× 20 调用（冗余，非实际调用数）
#          × +3k prompt 开销 ≈ 4.74M × ¥2/M（Flash 峰值·缓存未命中）= **¥9.48**
#   合计最坏 ≈ **¥46.2** → 硬上限取整 **¥47**。
# 余额 ¥42.80 < ¥47 → 跑前须充值 ≥ ¥5（代码预检强制）；实际输出远小于阀门时
# 真实花费将远低于此界（60s 哨兵实时盯账）；建议排非高峰时段（输出费减半）。
PRICE_IN_CNY_PER_M = 2.0    # Flash 高峰·缓存未命中（空闲 1 元）
PRICE_OUT_CNY_PER_M = 8.0   # Flash 高峰输出（空闲 4 元）
MAX_COST_CNY = 47.0         # = 最坏情况界 ¥46.2 取整（codex 口径；改动须走 PR）
ALERT_COST_CNY = 23.5       # 50%


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


def spent_for_reviews(server_host: str, container: str,
                      review_ids: list[str]) -> tuple[int, int]:
    """按本批 review_id 精确圈账（Codex 勘正：时间戳口径会把并发用户审查
    算进回测费用，污染记录且可能误触发杀批）。远端统计脚本经 stdin 送入
    （不经命令行，免嵌套引号地狱）。
    """
    if not review_ids:
        return 0, 0
    remote_code = (
        "import sqlite3,json,sys\n"
        "con = sqlite3.connect('/tmp/agent-t-reviews.db')\n"
        "ids = sys.argv[1].split(',')\n"
        "marks = ','.join('?' for _ in ids)\n"
        "rows = con.execute(f'SELECT usage FROM llm_calls WHERE review_id IN"
        " ({marks})', ids).fetchall()\n"
        "ins = sum(json.loads(u[0]).get('input_tokens') or 0 for u in rows)\n"
        "outs = sum(json.loads(u[0]).get('output_tokens') or 0 for u in rows)\n"
        "print(json.dumps([ins, outs]))\n"
    )
    r = subprocess.run(
        ["ssh", server_host,
         f"docker exec -i {container} python3 - {','.join(review_ids)}"],
        input=remote_code, capture_output=True, text=True, check=True)
    i, o = json.loads(r.stdout.strip().splitlines()[-1])
    return int(i), int(o)


def read_review_ids(ids_path: pathlib.Path) -> list[str]:
    """读 run_eval 逐卷落盘的 review_id JSONL（增量安全：缺文件/半行容忍）。"""
    if not ids_path.exists():
        return []
    out = []
    for line in ids_path.read_text(encoding="utf-8").splitlines():
        try:
            rid = json.loads(line).get("review_id")
            if rid:
                out.append(str(rid))
        except json.JSONDecodeError:
            continue  # 半行（写入中）容忍
    return out


# 每卷调用数上界（与审查预算 18 对账：2+6+6+2+2=18；Codex P1 口径）
CALLS_PER_NODE = {
    "precheck": 2,
    "model_review": 6,
    "quality": 6,
    "objection": 2,
    "ask": 2,
}


def worst_case_output_tokens_per_contract() -> int:
    """单卷最坏输出 tokens——直接读 #137 阀门常量逐节点累加。

    阀门调大、新增节点或调用数变化而费用上限未同步 → 下游测试必红。"""
    from app.services.llm_client import (DEFAULT_MAX_OUTPUT_TOKENS,
                                         NODE_MAX_OUTPUT_TOKENS)
    return sum(calls * NODE_MAX_OUTPUT_TOKENS.get(node, DEFAULT_MAX_OUTPUT_TOKENS)
               for node, calls in CALLS_PER_NODE.items())


# 17 卷抽取文本实测合计（2026-10-22 机算；生产同构抽取）
TOTAL_CORPUS_CHARS = 186_134
PROMPT_OVERHEAD_TOK = 3_000


def worst_case_input_tokens(contracts: int = 17) -> int:
    """输入界（冗余口径，非实际调用数）：合同字数 × 1 token/字冗余
    × 20 调用冗余 × +3k prompt 开销。"""
    calls_per = 20  # 冗余口径（预算 18+余量），与费用单文本一致
    chars_per = TOTAL_CORPUS_CHARS / contracts
    return int(contracts * calls_per * (chars_per + PROMPT_OVERHEAD_TOK))


def worst_case_cost_cny(contracts: int = 17,
                        valves_override: dict[str, int] | None = None) -> float:
    """最坏情况总费用（输入+输出同一来源；运行时预检与测试共用）。

    valves_override：模拟调大阀门用（测试「阀门变而上限未同步」必红场景）。
    """
    out_per = worst_case_output_tokens_per_contract()
    if valves_override:
        from app.services.llm_client import (DEFAULT_MAX_OUTPUT_TOKENS,
                                             NODE_MAX_OUTPUT_TOKENS)
        valves = {**NODE_MAX_OUTPUT_TOKENS, **valves_override}
        out_per = sum(calls * valves.get(node, DEFAULT_MAX_OUTPUT_TOKENS)
                      for node, calls in CALLS_PER_NODE.items())
    return estimate_cost(worst_case_input_tokens(contracts),
                         out_per * contracts)


def preflight_cost_check(balance: float, contracts: int = 17) -> dict[str, float]:
    """费用预检（运行时唯一入口，测试直测本函数=覆盖真实预检路径）。

    两条拦截：①阀门漂移（硬上限<机算最坏）②余额不足。
    """
    worst_cost = worst_case_cost_cny(contracts)
    if MAX_COST_CNY < worst_cost:
        raise SystemExit(
            f"FAIL: 硬上限 ¥{MAX_COST_CNY} < 机算最坏 ¥{worst_cost:.2f}"
            f"——阀门/调用数已变，费用上限须走 PR 同步")
    if balance < MAX_COST_CNY:
        raise SystemExit(
            f"FAIL: 余额 ¥{balance} < 最坏情况界 ¥{MAX_COST_CNY}"
            f"——先充值 ≥ ¥{MAX_COST_CNY - balance:.2f} 再跑（费用上限单见记录）")
    return {"worst_cost_cny": round(worst_cost, 2),
            "hard_cap_cny": MAX_COST_CNY, "balance_cny": balance}


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
            "模型依据": "生产 env DEEPSEEK_MODEL=deepseek-v4-flash（用户确认）；"
                        "官方注：旧名由 V4.1-Flash-1017 服务、按 Flash 价格计费",
            "牌价依据": "官方中文页 Flash 列峰值：输入 ¥2/M（缓存未命中）、输出 ¥8/M"
                        "（空闲半价；api-docs.deepseek.com/zh-cn/quick_start/pricing，"
                        "2026-10-22 抓取原文）",
            "最坏情况界": "¥46.2（逐节点公式 worst_case_output_tokens_per_contract()"
                          "=270k/卷 ×17 卷 ×¥8=¥36.72；输入 4.74M×¥2=¥9.48——"
                          "20 次为冗余非实际调用数）；非高峰时段跑输出费减半，"
                          "实际输出远小于阀门时更低",
            "硬上限人民币": MAX_COST_CNY,
            "告警线人民币": ALERT_COST_CNY,
            "超限方式": "跑前余额≥硬上限否则拒绝；跑中每 60s 按生产账本累计 usage"
                        " 实时核算，≥告警线打印、≥硬上限终止子进程；末次结算必做，"
                        "实际花费超界→记录标记「结论作废待人工复核」",
            "deepseek_balance_cny": balance,
        }
        # 费用预检：唯一入口=preflight_cost_check（与测试共用同一函数——
        # Codex P1：预检路径必须被测试覆盖，不许第二套手搓算法）
        record["cost_preflight"] = preflight_cost_check(balance, len(roster))
        print(f"[费用上限单] 硬上限(最坏情况界) ¥{MAX_COST_CNY} / 告警 ¥{ALERT_COST_CNY}"
              f" / 余额 ¥{balance}（充足，无需充值）")
        names = ",".join(roster)
        run_out = str(pathlib.Path(args.out).parent / "final-replay-run.json")
        ids_out = str(pathlib.Path(args.out).parent / "final-replay-review-ids.jsonl")
        pathlib.Path(ids_out).unlink(missing_ok=True)  # 增量文件清零开跑
        cmd = [sys_exec(), "-X", "utf8", "tools/m65/run_eval.py",
               "--fixtures", str(fixtures), "--names", names,
               "--out", run_out, "--base", args.base,
               # Codex 勘正：auth-file 必须透传子进程（否则子进程回退自己的
               # 默认路径，操作者自定义的凭据文件被静默忽略）
               "--auth-file", args.auth_file,
               # Codex 勘正②：review_id 逐卷落盘——费用哨兵按本批 review_id
               # 精确圈账，不把并发用户审查算进回测
               "--review-ids-out", ids_out]
        if args.insecure:
            cmd.append("--insecure")
        print("全集跑批（约 40-60 分钟，DeepSeek 计费）：", " ".join(cmd[2:]))
        proc = subprocess.Popen(cmd)
        while proc.poll() is None:
            time.sleep(60)
            if proc.poll() is not None:
                break
            try:
                i_tok, o_tok = spent_for_reviews(
                    args.server_host, "agent-t",
                    read_review_ids(pathlib.Path(ids_out)))
                cost = estimate_cost(i_tok, o_tok)
                print(f"[费用哨兵] 已用 入{i_tok}/出{o_tok} tokens ≈ ¥{cost:.2f}")
                if cost >= MAX_COST_CNY:
                    proc.kill()
                    # Codex P1：上传接口返回后服务端 daemon 审查线程仍在跑——
                    # kill 客户端不停账。排空在途审查（有界：每卷受 llm_budget
                    # 18 调用+阀门双重限制）再结算，溢出部分如实记账。
                    print("[费用哨兵] 达硬上限：客户端已终止，等待服务端在途审查排空…")
                    prev = (i_tok, o_tok)
                    settled_rounds, deadline = 0, time.time() + 600
                    while time.time() < deadline and settled_rounds < 2:
                        time.sleep(60)
                        cur = spent_for_reviews(
                            args.server_host, "agent-t",
                            read_review_ids(pathlib.Path(ids_out)))
                        growth = (cur[0] - prev[0]) + (cur[1] - prev[1])
                        overshoot = max(estimate_cost(*cur) - MAX_COST_CNY, 0.0)
                        print(f"[排空] 累计 ¥{estimate_cost(*cur):.2f}"
                              f"（超限溢出 ¥{overshoot:.2f}）增量 {growth} tokens")
                        settled_rounds = settled_rounds + 1 if growth == 0 else 0
                        prev = cur
                    raise SystemExit(
                        f"FAIL: 费用 ¥{estimate_cost(*prev):.2f} ≥ 硬上限 "
                        f"¥{MAX_COST_CNY}——已终止并排空在途审查，"
                        f"溢出 ¥{overshoot:.2f} 已记账")
                if cost >= ALERT_COST_CNY:
                    print(f"[费用告警] ¥{cost:.2f} ≥ 告警线 ¥{ALERT_COST_CNY}")
            except SystemExit:
                raise
            except Exception as exc:  # 哨兵自身故障不误杀跑批，打印继续
                print("[费用哨兵] 查询失败（不终止）：", type(exc).__name__)
        if proc.returncode != 0:
            raise SystemExit(f"FAIL: run_eval 退出码 {proc.returncode}")
        # 末次结算必做（Codex P1：最后一轮检查后的花费不流失）
        i_fin, o_fin = spent_for_reviews(
            args.server_host, "agent-t",
            read_review_ids(pathlib.Path(ids_out)))
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
