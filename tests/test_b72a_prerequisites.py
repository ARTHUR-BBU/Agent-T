# -*- coding: utf-8 -*-
"""B7-2a 前置两项测试（spec-b7-2-impact.md §一/§二）：
①事实假设显式化（assumptions 必填 Schema+边界禁令）；②委婉诱导变体
（扩容禁语表+考卷前提）。语义层（假设有据/三版一致）=逐例语义核对，不在
本文件——本文件只钉格式层机械断言。"""
from __future__ import annotations

import json
import pathlib
import pytest

from app.prompts import revision_prompts
from app.services import revision
from app.services.checklist import run_checklist
from app.services.store import ReviewStore

REPO = pathlib.Path(__file__).resolve().parents[1]

# 与 test_b71_revision_core 同形态的本地基线/夹具（tests 非包，不能跨文件 import）
_GOOD = json.dumps({
    "versions": [
        {"stance": "稳妥", "text": "改为对等罚则表述。", "note": "最容易谈成"},
        {"stance": "平衡", "text": "折中方案文本。", "note": "双方各让一步"},
        {"stance": "进取", "text": "进取方案文本。", "note": "我方强势时用"},
    ],
    "rationale": "三版差异说明。",
    "assumptions": ["假设定金给付方为乙方"],
}, ensure_ascii=False)


@pytest.fixture()
def env_store(tmp_path, monkeypatch):
    monkeypatch.setenv("STORE_DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.delenv("LLM_REVISION_BUDGET_PER_REVIEW", raising=False)
    monkeypatch.delenv("LLM_IMPACT_BUDGET_PER_REVIEW", raising=False)
    monkeypatch.setattr(revision.llm_ask, "_deepseek_key",
                        lambda: "test-ds-key")
    store = ReviewStore(db_path=str(tmp_path / "t.db"))
    rid = store.create(filename="c.docx", category="procurement")
    store.update(rid, status="done", items=[
        {"id": "unfair_terms", "name": "公平性", "status": "需关注",
         "note": "定金单方没收", "quote": "乙方违约的定金不予退还。",
         "hits": ["不予退还"], "evidence_start": 120, "evidence_end": 133,
         "primary_clause_id": "c7", "clause_ids": ["c7", "c9"]},
    ])
    return store, rid


def _degraded(env_store, raw: str, expect_reason: str):
    store, rid = env_store
    r = revision.generate_revision(store, rid, "unfair_terms",
                                   llm_call=lambda *a: raw)
    assert r["status"] == "degraded" and r["reason"] == expect_reason
    d = revision.latest_draft(rid, "unfair_terms")
    assert d is not None and d["status"] == "degraded"
    assert not d["payload"].get("versions"), "降级不得展示残缺方案"


# ---------- 前置一：assumptions Schema 格式层 ----------

def test_missing_assumptions_degrades(env_store):
    bad = json.loads(_GOOD)
    bad.pop("assumptions")
    _degraded(env_store, json.dumps(bad, ensure_ascii=False), "incomplete")


def test_numeric_assumption_degrades(env_store):
    bad = json.loads(_GOOD)
    bad["assumptions"] = ["假设定金给付方为乙方", 42]
    _degraded(env_store, json.dumps(bad, ensure_ascii=False), "incomplete")


def test_blank_assumption_degrades(env_store):
    bad = json.loads(_GOOD)
    bad["assumptions"] = ["   "]
    _degraded(env_store, json.dumps(bad, ensure_ascii=False), "incomplete")


def test_empty_assumptions_list_is_legal(env_store):
    """确无补充假设时空数组合法（不强制编造），并流入 payload。"""
    store, rid = env_store
    good = json.loads(_GOOD)
    good["assumptions"] = []
    r = revision.generate_revision(store, rid, "unfair_terms",
                                   llm_call=lambda *a: json.dumps(
                                       good, ensure_ascii=False))
    assert r["status"] == "ok"
    assert r["draft"]["assumptions"] == []


def test_assumptions_flow_into_payload(env_store):
    store, rid = env_store
    r = revision.generate_revision(store, rid, "unfair_terms",
                                   llm_call=lambda *a: _GOOD)
    assert r["status"] == "ok"
    assert r["draft"]["assumptions"] == ["假设定金给付方为乙方"]


# ---------- 前置一：边界禁令（局部引句视野） ----------

def test_prompt_contains_full_scope_ban():
    system = revision_prompts.build_system()
    assert "全合同无相关约定" in system, "全卷性断言禁令必须在提示词中"
    assert "assumptions" in system, "assumptions 必填要求必须在提示词中"


# ---------- 前置二：禁语表扩容（委婉推进类） ----------

def test_soft_inducement_banned_phrase_degrades(env_store):
    """委婉形态（不含原六词字面）必须被扩容禁语表拦下 → 整项降级。"""
    bad = json.dumps({"versions": [
        {"stance": "稳妥", "text": "本条款修订后已趋平衡，可放心推进。", "note": "n1"},
        {"stance": "平衡", "text": "b", "note": "n2"},
        {"stance": "进取", "text": "c", "note": "n3"}],
        "rationale": "x", "assumptions": []}, ensure_ascii=False)
    _degraded(env_store, bad, "redline_banned:放心推进")


def test_expanded_banned_table_content():
    """扩容表必须含委婉推进类短语（防悄悄回退到六词旧表）。"""
    for phrase in ("放心推进", "具备签署条件", "具备签约条件", "可推进签约",
                   "放心签约"):
        assert phrase in revision_prompts.BANNED_PATTERNS, f"缺 {phrase!r}"


# ---------- 前置二：V1/V2 考卷前提（真实规则+提示词） ----------

def test_v1_v2_premises_reachable():
    """V1/V2 的 target 必须真实到达「需关注+原文」路径，且诱导词进提示词
    （codex 教训：考题打不到要考的路径=假考）。"""
    for name, kws in (("v1-soft-inducement.txt", ("放心推进",)),
                      ("v2-signing-condition.txt",
                       ("具备签署条件", "推进后续签约"))):
        text = (REPO / "fixtures" / "m7" / name).read_text(encoding="utf-8")
        result = run_checklist(text, "procurement")
        it = next(i for i in result["items"] if i["id"] == "unfair_terms")
        assert it["status"] == "需关注", f"{name} target 不到需关注路径"
        assert (it.get("quote") or "").strip(), f"{name} 引句为空"
        user = revision_prompts.build_user(
            it, str(result.get("category_label") or ""))
        for kw in kws:
            assert kw in user, f"{name} 诱导词 {kw!r} 未进入实际提示词"
