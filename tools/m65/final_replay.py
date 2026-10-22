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
"""
from __future__ import annotations

import argparse
import hashlib
import json
from typing import Any
import pathlib
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
            paras = [p.text.strip() for p in
                     __import__("docx").Document(str(path)).paragraphs
                     if spec["term"] in p.text]
            rec["paragraph_hits"] = paras
            rec["autopsy_verdict"] = (f"正文实锤（{len(paras)} 处）" if paras
                                      else "正文与表格均未读到——抽取缺口嫌疑")
        records.append(rec)
    return records


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fixtures", default="fixtures-real")
    ap.add_argument("--out", default="docs/m65/final-replay.json")
    ap.add_argument("--base", default="http://59.110.13.13:8080")
    ap.add_argument("--insecure", action="store_true")
    ap.add_argument("--skip-llm", action="store_true",
                    help="只出靶向证据链与骨架（不跑 LLM 全集，供模板预览）")
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
    print("冻结哈希:", record["git_hash"], "| 名册:", len(roster), "卷")

    # ① 靶向证据链（本地抽取层，零 LLM 成本）
    record["evidence_chain"] = evidence_chain(fixtures)
    for r in record["evidence_chain"]:
        print(f"[靶向] {r['paper']} 「{r['term']}」: {r['autopsy_verdict']}"
              f" 位置={r.get('table_position') or r.get('paragraph_hits')}")

    # ② LLM 全集跑批（委托既有跑道；成本需用户批准后执行）
    if not args.skip_llm:
        names = ",".join(roster)
        run_out = str(pathlib.Path(args.out).parent / "final-replay-run.json")
        cmd = [sys_exec(), "-X", "utf8", "tools/m65/run_eval.py",
               "--fixtures", str(fixtures), "--names", names,
               "--out", run_out, "--base", args.base]
        if args.insecure:
            cmd.append("--insecure")
        print("全集跑批（约 40-60 分钟，DeepSeek 计费）：", " ".join(cmd[2:]))
        subprocess.run(cmd, check=True)
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
