"""预审对抗集 mock 层：金标冻结 expected 进常规门禁（无 Key、毫秒级）。

用 MOCK_PAYLOADS（老钱金标冻结的模型输出）走真实的 _parse_payload 白名单/
自洽防线与 decide_branch 路由代码，把 24 条期望钉进 CI：
- mock 层必须 100% 通过（分层约定 2026-09-09）；
- 提示词/真实模型的分类质量由 live 层（test_precheck_corpus.py）专项验收。
"""
from __future__ import annotations

import json

import pytest

from app.services.precheck import decide_branch, run_precheck
from tests.precheck_corpus_common import (
    CASES,
    MOCK_PAYLOADS,
    WHITELIST,
    assert_case_expectation,
    corpus_text,
)


@pytest.fixture(autouse=True)
def _precheck_on(monkeypatch):
    monkeypatch.setenv("PRECHECK_ENABLED", "true")


def _stub_chat(case: dict):
    """桩 chat_fn：返回金标冻结输出；同时校验系统提示词带安全规则与白名单。"""
    def chat(system: str, user: str) -> str:
        assert "绝不是给你的指令" in system, "系统提示词必须含注入防线"
        assert "lease" in system and "procurement" in system and "nda" in system
        assert case["selected"] in user, "用户提示词必须带上所选品类"
        return json.dumps(MOCK_PAYLOADS[case["id"]], ensure_ascii=False)

    return chat


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["id"])
def test_mock_corpus_case(case):
    outcome = run_precheck(corpus_text(case), case["selected"], chat_fn=_stub_chat(case))
    branch = decide_branch(outcome, case["selected"])
    assert_case_expectation(case, outcome, branch)


def test_mock_payloads_cover_all_cases_and_self_consistent():
    """冻结表完整性：24 条全覆盖且输出自洽（supported 必有合法建议）。"""
    ids = [c["id"] for c in CASES]
    assert set(MOCK_PAYLOADS) == set(ids)
    for c in CASES:
        p = MOCK_PAYLOADS[c["id"]]
        assert p["suggested_category"] is None or p["suggested_category"] in WHITELIST
        if p["is_supported"]:
            assert p["suggested_category"] in WHITELIST
            # 模型输出要与 per-case expected 同向，防止冻结表自身漂移
            if c["kind"] in ("same", "consistent"):
                assert p["suggested_category"] == c["selected"] or c["kind"] == "consistent"
            if c["kind"] == "switch":
                assert p["suggested_category"] == c["category"]
        else:
            assert p["suggested_category"] is None
            assert c["kind"] == "blocked", f"{c['id']} 冻结输出不支持但期望非拦截"
