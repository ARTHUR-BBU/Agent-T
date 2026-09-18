"""阶段 3 异议层 live 层：真实 LLM 评测（三轮审计 F 项）。

与预审对抗集 live 层同模式：@pytest.mark.live_llm，无 Key 整模块 skip，
不进 required CI——作为发布前 / 每日 / 模型版本切换时的验收套件。
跑法：PRECHECK_LIVE=1 python -X utf8 -m pytest tests/test_objection_live_corpus.py

mock 层（test_objection_corpus.py）回答「模型给我这种输出我能不能安全处理」；
本层回答「真实 DeepSeek/GLM 到底能不能提出正确异议」——换模型时先跑这里，
才知道「代码没变，律师脑子变了没有」。

记录指标（审计第十二节清单）：
- available 率 / 调用量（每份合同 1-2 次，预算账目）
- accepted / rejected 分布 + 拒收原因分布
- forbidden output rate（红线 0 容忍：服务端已 scrub，本层双保险断言）
- quote 核验通过率（accepted 的 quote 天然全部过五要件①，此处断言即可）

召回率/误报率金标（哪些该提异议）尚未建立——老钱出题后接入
assert_case_expectation 模式，本层先固定**红线断言 + 指标采集**。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from app.services import objection as objection_service
from app.services.blind_spot import annotate_rule_items
from app.services.checklist import run_checklist
from app.services.clause_index import build_clause_index
from tests.precheck_corpus_common import CASES, BANNED

ROOT = Path(__file__).resolve().parents[1]


def _resolve_key():
    """env 优先；.env 兜底需显式 PRECHECK_LIVE=1（防误烧真实 Key）。
    返回 (env变量名, key)——供应商变量名必须原样保留（智谱 Key 塞进
    DEEPSEEK_API_KEY 会 401，首轮实测踩过）。"""
    order = ["DEEPSEEK_API_KEY", "ZHIPU_API_KEY", "GLM_API_KEY"]
    for name in order:
        v = os.getenv(name)
        if v:
            return name, v
    live_opt_in = os.getenv("PRECHECK_LIVE", "").strip().lower() not in {"", "0", "false", "no"}
    if live_opt_in:
        envf = ROOT / ".env"
        if envf.exists():
            for line in envf.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    if k.strip() in order and v.strip():
                        return k.strip(), v.strip()
    return None, None


KEY_NAME, KEY = _resolve_key()

pytestmark = [
    pytest.mark.live_llm,
    pytest.mark.skipif(not KEY, reason="无 LLM Key：live 层整模块 skip（验收专项跑）"),
]

# 三品类各取一份代表语料（precheck 正例组，真实合同语料）
LIVE_CASES = [c for c in CASES if c["file"] in {
    "pc01_lease_house.txt", "pc05_proc_standard_goods.txt", "pc07_nda_mutual.txt",
}]


@pytest.mark.parametrize("case", LIVE_CASES, ids=[c["id"] for c in LIVE_CASES])
def test_live_objection_quality_and_red_lines(case, monkeypatch):
    """真实 LLM 跑异议层：红线 0 容忍 + 指标采集（金标建立前不断言召回）。"""
    monkeypatch.setenv("OBJECTIONS_ENABLED", "true")  # conftest autouse 默认关
    # Key 从 .env 取出后须注入环境：run_objections 的 _default_chat_fn 只读 os.environ
    if not os.getenv(KEY_NAME):
        monkeypatch.setenv(KEY_NAME, KEY)
    text = (ROOT / "fixtures" / "precheck" / case["file"]).read_text(encoding="utf-8")
    result = run_checklist(text, case["selected"])
    items = annotate_rule_items(result["items"])
    snapshot = [dict(it) for it in items]

    out = objection_service.run_objections(
        text=text, items=items, clause_index=build_clause_index(text),
    )

    # ---- 红线（0 容忍）----
    assert out["available"] is True, f"{case['id']}: live 层不得降级（预算/解析失败要查）"
    for o in out["objections"]:
        assert o["needs_confirm"] is True, "needs_confirm 必须代码强制"
        blob = json.dumps(o, ensure_ascii=False)
        for banned in BANNED:
            assert banned not in blob, f"{case['id']}: 禁语出库 {banned!r}"
        if o["accepted"]:
            assert o["clause_id"] or o["clause_ambiguous"], "受理条目必须有条款定位"
    # 铁律 3 live 级：规则档位一个字不动
    assert items == snapshot, f"{case['id']}: 规则档位被异议层改动（铁律 3 破线）"

    # ---- 指标采集（金标建立前仅打印，接入老钱金标后升级为断言）----
    cov = out.get("coverage") or {}
    accepted = [o for o in out["objections"] if o["accepted"]]
    rejected = [o for o in out["objections"] if not o["accepted"]]
    print(
        f"\n[{case['id']}] live 指标：eligible={cov.get('eligible')} "
        f"accepted={len(accepted)} rejected={len(rejected)} "
        f"body_limited={cov.get('body_limited')} "
        f"拒收原因={[o.get('reject_reason', '')[:12] for o in rejected]}"
    )
