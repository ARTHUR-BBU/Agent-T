"""跨品类对抗集 live 层：真实 LLM 实测（小智娘设计）。

与 mock 层（test_precheck_corpus_mock.py，常规门禁）共用 CASES 与断言
（tests/precheck_corpus_common.py，唯一权威版本）。

分层约定（2026-09-09 与老王对齐）：
- 本模块标记 @pytest.mark.live_llm，无 Key 整模块 skip，验收专项跑，不进 CI 门禁；
- 门控顺序：DEEPSEEK_API_KEY > ZHIPU/GLM_API_KEY > 仓库 .env（仅本地实测用）；
- 实际供应商在报告中注明（供应商差异会让结论不可比）。

通过率标准：正例 9 条 100%（confidence>=medium，0 误打扰）；不支持 5 条 +
注入 #22/#23 红线 0 容忍；容忍带分类错 0 条；总体 >=23/24 且红线全绿。
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.precheck_corpus_common import (
    CASES,
    WHITELIST,
    assert_case_expectation,
    corpus_text,
)

ROOT = Path(__file__).resolve().parents[1]


def _resolve_key():
    """env 优先；.env 兜底需显式 PRECHECK_LIVE=1（防止误烧真实 Key）。"""
    order = [
        ("deepseek", "DEEPSEEK_API_KEY"),
        ("zhipu", "ZHIPU_API_KEY"),
        ("zhipu", "GLM_API_KEY"),
    ]
    for vendor, name in order:
        v = os.getenv(name)
        if v:
            return vendor, name, v
    # .env 兜底必须显式 opt-in（PRECHECK_LIVE=1）：本地全量 pytest 默认不触发
    # 真实调用；跑 live 层请 PRECHECK_LIVE=1 python -m pytest tests/test_precheck_corpus.py
    live_opt_in = os.getenv("PRECHECK_LIVE", "").strip().lower() not in {"", "0", "false", "no"}
    if live_opt_in:
        envf = ROOT / ".env"
        if envf.exists():
            pairs = {}
            for line in envf.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    pairs[k.strip()] = v.strip()
            for vendor, name in order:
                if pairs.get(name):
                    return vendor, name, pairs[name]
    return None, None, None


VENDOR, KEY_NAME, KEY = _resolve_key()

pytestmark = [
    pytest.mark.live_llm,
    pytest.mark.skipif(
        KEY is None,
        reason="live_llm：需真实 LLM Key（DEEPSEEK/ZHIPU），未检出则整模块跳过",
    ),
]


@pytest.fixture(autouse=True)
def _real_llm_env(monkeypatch):
    """把解析到的 Key 注入进程 env（预审走 llm_ask 供应商链）+ 打开预审。"""
    if KEY:
        monkeypatch.setenv(KEY_NAME, KEY)
    monkeypatch.setenv("PRECHECK_ENABLED", "true")


def _run(text: str, selected: str):
    from app.services.precheck import decide_branch, run_precheck

    outcome = run_precheck(text, selected)  # 不传 chat_fn：走真实供应商链
    branch = decide_branch(outcome, selected)
    return outcome, branch


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["id"])
def test_live_corpus_case(case):
    """live：真实 Key 逐条分类。per-case expected 见共用模块（金标冻结）。"""
    outcome, branch = _run(corpus_text(case), case["selected"])
    assert_case_expectation(case, outcome, branch)


# ---------- 真实 Key 下的 API 级红线（各抽 1 条） ----------

def test_live_api_service_contract_blocked_without_review_id():
    """pc10 传采购：category_confirm 且绝无 review_id（确认分支不开审）。"""
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    payload = (ROOT / "fixtures/precheck/pc10_service_video_production.txt").read_bytes()
    r = client.post(
        "/api/upload",
        files={"file": ("pc10.txt", payload, "text/plain")},
        data={"category": "procurement"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "category_confirm"
    assert body["review_id"] is None, "confirm 分支绝不能创建 review"
    assert body["suggested_category"] is None
    assert body["precheck"]["detected_type"]
    assert {c["id"] for c in body["supported_categories"]} == WHITELIST


def test_live_api_correct_category_undisturbed_and_rule_pure():
    """pc01 选 lease：正常开审，预审不污染规则层（tag_source 全部 rule）。"""
    from fastapi.testclient import TestClient

    from app.main import app
    from tests.helpers import wait_review_done

    client = TestClient(app)
    payload = (ROOT / "fixtures/precheck/pc01_lease_house.txt").read_bytes()
    r = client.post(
        "/api/upload",
        files={"file": ("pc01.txt", payload, "text/plain")},
        data={"category": "lease"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "uploaded", f"正确品类被误打扰：{body}"
    assert body["review_id"]
    review = wait_review_done(client, body["review_id"], timeout=300)
    assert review["status"] == "done", review.get("error")
    # 同品类 => 无 suspect 提示
    assert review["precheck"] is None
    # 规则层纯净：任何条目都不得被预审改标
    for item in review["items"]:
        assert item.get("tag_source") in ("rule", None), item
