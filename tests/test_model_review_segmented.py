"""分段阅读（阶段 1.2）测试：短路红线、map-reduce 管道、预算、降级链。

红线：≤6000 字走历史单调用路径（calls==1）；分段路径下禁语重试/降级/
封顶全链与单调用路径同构；补盲 quote 仍对全文校验。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services import model_review, scorecard
from app.services.clause_index import build_clause_index
from app.services.llm_budget import ReviewBudget

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "procurement_long_backtoback.txt"

ITEMS = [
    {"id": "payment", "name": "付款条款", "segment": "C", "status": "通过", "note": "", "hits": [], "quote": ""},
    {"id": "breach", "name": "违约责任", "segment": "D", "status": "需关注", "note": "逾期违约金未约定", "hits": [], "quote": ""},
]


def _reduce_payload(summary: str = "规则结果汇总。", comment_c: str = "付款条款按规则结果展开") -> str:
    return json.dumps(
        {
            "scorecard": {
                "summary": summary,
                "segments": [
                    {"key": "C", "score": 8, "comment": comment_c, "gap_item_ids": []},
                    {"key": "D", "score": 10, "comment": "违约责任条款按规则结果展开", "gap_item_ids": []},
                ],
            },
            "candidates": [],
        },
        ensure_ascii=False,
    )


def _banned_reduce_payload() -> str:
    return _reduce_payload(comment_c="这份合同没有问题，可以放心签署")


def _map_observations(comment: str = "第五条出现背靠背条款：以业主付款为前提", gap_ids=None, quote: str = "") -> str:
    return json.dumps(
        {
            "observations": [
                {
                    "segment": "C",
                    "comment": comment,
                    "gap_item_ids": gap_ids or [],
                    "candidates": (
                        [{"item_id": "payment", "name": "付款条款", "note": "中段风险", "quote": quote}]
                        if quote
                        else []
                    ),
                }
            ]
        },
        ensure_ascii=False,
    )


def _chat_router(reduce_outputs: list[str], map_output: str | Exception | None = None):
    """system prompt 含「分段阅读」→ map 轮；否则 reduce 轮。记录全部调用。"""
    calls: list[tuple[str, str]] = []
    reduce_iter = iter(reduce_outputs)

    def chat(system: str, user: str) -> str:
        calls.append((system, user))
        if "分段阅读" in system:
            if isinstance(map_output, Exception):
                raise map_output
            return map_output if map_output is not None else json.dumps({"observations": []}, ensure_ascii=False)
        return next(reduce_iter)

    return chat, calls


def _run(text: str, chat, **kw):
    return model_review.run_model_review(
        text=text, items=ITEMS, policies=[], category="procurement", chat_fn=chat, **kw
    )


@pytest.fixture(autouse=True)
def _blind_on(monkeypatch):
    monkeypatch.setenv("BLIND_SPOT_ENABLED", "true")


# ---------- 短路红线 ----------

def test_exactly_6000_chars_stays_single_call():
    """回归红线：6000 字整（含）以内必须仍走历史单调用路径。"""
    chat, calls = _chat_router([_reduce_payload()])
    text = "甲乙双方约定设备采购事宜。" + "细则条款内容。" * 857  # ≈6000 字
    text = text[:6000]
    result = _run(text, chat)
    assert len(calls) == 1, "6000 字整不得触发分段路径"
    assert "合同全文" in calls[0][1], "单调用路径的 user prompt 形状不得变化"
    assert result["scorecard"]["available"] is True


# ---------- map-reduce 管道 ----------

def _fixture_text() -> str:
    return FIXTURE.read_text(encoding="utf-8")


def test_long_contract_uses_map_reduce():
    text = _fixture_text()
    assert len(text) > scorecard.MAX_CONTRACT_CHARS
    chat, calls = _chat_router([_reduce_payload()])
    result = _run(text, chat)
    # 7474 字 → 2 个阅读块（map ×2）+ 汇总 1 次
    assert len(calls) == 3, f"map(2)+reduce(1)，实际 {len(calls)}"
    assert "分段阅读" in calls[0][0], "前 N-1 次必须是 map 轮"
    assert "分段阅读" not in calls[-1][0], "最后一次必须是 reduce 汇总轮"
    assert "合同全文" not in calls[-1][1], "reduce 不得携带合同全文（延迟护栏）"
    assert result["scorecard"]["available"] is True


def test_chunking_keeps_mid_doc_backtoback_in_some_chunk():
    """1.2 验收的块级前提：中段背靠背条款必须落在某个阅读块里（条款对齐切块）。"""
    text = _fixture_text()
    idx = build_clause_index(text)
    chunks = scorecard.build_review_chunks(text, idx, max_segments=4)
    assert any("背靠背" in c for c in chunks), "中段背靠背条款不得被切块丢掉"
    assert all(len(c) <= scorecard.MAX_CONTRACT_CHARS for c in chunks)


def test_clause_aligned_chunks_do_not_split_a_clause():
    text = (
        "第一条 甲\n" + "内容一。" * 400 + "\n"
        "第二条 乙\n" + "内容二。" * 400 + "\n"
        "第三条 丙\n" + "内容三。" * 400
    )
    idx = build_clause_index(text)
    chunks = scorecard.build_review_chunks(text, idx, max_segments=4)
    joined = "".join(chunks)
    assert joined == text, "切块必须覆盖全文且不重不漏"
    for clause_body in ("内容一。", "内容二。", "内容三。"):
        assert all(clause_body in c for c in chunks if clause_body in c) or any(
            chunk.count(clause_body) == text.count(clause_body) for chunk in chunks
        )


def test_reduce_prompt_carries_map_observations():
    """1.2 验收：中段条款经 map 观察进入 reduce 素材块（管道贯通）。"""
    text = _fixture_text()
    chat, calls = _chat_router(
        [_reduce_payload(summary="发现背靠背等风险。")],
        map_output=_map_observations(quote="以业主单位向甲方实际支付相应工程款作为前提条件"),
    )
    result = _run(text, chat)
    reduce_user = calls[-1][1]
    assert "背靠背" in reduce_user, "map 观察素材必须进 reduce prompt"
    assert "以业主单位向甲方实际支付相应工程款作为前提条件" in reduce_user
    assert result["scorecard"]["available"] is True
    assert "背靠背" in result["scorecard"]["summary"]


# ---------- 禁语 / 降级链同构 ----------

def test_reduce_forbidden_hit_retries_once_then_clean():
    text = _fixture_text()
    chat, calls = _chat_router(
        [_banned_reduce_payload(), _reduce_payload()],
        map_output=_map_observations(),
    )
    result = _run(text, chat)
    assert len(calls) == 4, "map(2) + reduce(1) + 禁语重试(1)"
    assert "再次提醒" in calls[-1][0], "重试轮必须带加严提醒"
    assert result["scorecard"]["available"] is True
    assert not result["scorecard"].get("degraded")


def test_reduce_forbidden_twice_degrades_to_score_only():
    text = _fixture_text()
    chat, calls = _chat_router(
        [_banned_reduce_payload(), _banned_reduce_payload()],
        map_output=_map_observations(),
    )
    result = _run(text, chat)
    assert len(calls) == 4
    assert result["scorecard"]["degraded"] is True
    assert result["scorecard"]["segments"][0]["comment"] == "", "二次禁语必须清空评语"


def test_map_observation_scrubs_forbidden_before_reduce():
    """map 素材进门过禁语清洗：禁语不得经素材回流放大。"""
    block = scorecard.format_observations(
        [{"segment": "C", "comment": "该条款这份合同没有问题", "gap_item_ids": [], "candidates": []}]
    )
    assert "没有问题" not in block
    assert "【已过滤】" in block


# ---------- 降级与预算 ----------

def test_map_all_fail_falls_back_to_clipped_single_call():
    text = _fixture_text()
    chat, calls = _chat_router([_reduce_payload()], map_output=RuntimeError("map 挂了"))
    result = _run(text, chat)
    assert len(calls) == 3, "map 2 次全败 + 回退单调用 1 次"
    assert "合同全文" in calls[-1][1], "回退路径必须走头尾采样的历史 user prompt"
    assert result["scorecard"]["available"] is True


def test_map_budget_stops_early_reduce_still_runs():
    """map 中途预算尽：已收观察直接进 reduce，reduce 额度优先保障。"""
    text = _fixture_text()
    chat, calls = _chat_router([_reduce_payload()], map_output=_map_observations())
    result = _run(text, chat, budget=ReviewBudget(3))
    assert len(calls) == 3, "map 2 + reduce 1 = 预算 3 恰好用满"
    assert result["scorecard"]["available"] is True


def test_budget_of_one_goes_straight_to_reduce():
    """预算 1：map 预留逻辑直接跳过全部 map，唯一额度保 reduce。"""
    text = _fixture_text()
    chat, calls = _chat_router([_reduce_payload()], map_output=_map_observations())
    result = _run(text, chat, budget=ReviewBudget(1))
    assert len(calls) == 1, "0 次 map + 1 次 reduce"
    assert "分段阅读" not in calls[0][0]
    assert result["scorecard"]["available"] is True


# ---------- 补盲联动 ----------

def test_map_gap_ids_feed_blind_targets(monkeypatch):
    """map 点名的缺口并入补盲靶点：中段「通过但表述弱」也进定向补盲。"""
    captured = {}

    def fake_select(items, named_ids):
        captured["named"] = list(named_ids)
        return []

    monkeypatch.setattr(model_review.blind_spot, "select_target_gaps", fake_select)
    text = _fixture_text()
    chat, _ = _chat_router(
        [_reduce_payload()],
        map_output=_map_observations(gap_ids=["payment"]),
    )
    _run(text, chat)
    assert "payment" in captured["named"], "map gap ids 必须并入补盲靶点"


# ---------- build_review_chunks 单元 ----------

def test_build_review_chunks_caps_at_max_segments():
    text = "超" * 25000
    chunks = scorecard.build_review_chunks(text, None, max_segments=4)
    assert len(chunks) <= 4
    assert all(len(c) <= scorecard.MAX_CONTRACT_CHARS for c in chunks)
    assert sum(len(c) for c in chunks) >= 4 * scorecard.MAX_CONTRACT_CHARS - scorecard.MAX_CONTRACT_CHARS


def test_build_review_chunks_empty_text():
    assert scorecard.build_review_chunks("", None) == []


# ---------- 门禁整改回归 ----------

def test_corrupted_clause_index_falls_back_to_paragraph_ranges():
    """门禁 P1-1 防御纵深：塌缩/重叠/低覆盖的坏索引不得被分段路径采用。"""
    text = "甲方应按约供货，逾期每日按千分之一支付违约金。" * 100
    bad_index = {
        "strategy": "paragraph",
        "count": 2,
        "clauses": [
            {"id": "c01", "heading": "x", "start": 0, "end": 50, "chars": 50},
            {"id": "c02", "heading": "x", "start": 4, "end": 69, "chars": 65},
        ],
    }
    chunks = scorecard.build_review_chunks(text, bad_index, max_segments=4)
    covered = sum(len(c) for c in chunks)
    assert covered >= 0.9 * len(text), "坏索引必须回退段落聚合，不得静默漏读正文"


def test_tiny_tail_chunk_merged_not_wasted():
    """门禁 P3 挂账④：碎尾块并入前块，不白耗一次 map 调用与预算。"""
    # 1 字尾块场景：6000+1 字无换行 → 硬切后又并回，单块 6001 字
    text = "超" * (scorecard.MAX_CONTRACT_CHARS + 1)
    chunks = scorecard.build_review_chunks(text, None, max_segments=4)
    assert len(chunks) == 1 and len(chunks[0]) == scorecard.MAX_CONTRACT_CHARS + 1

    # 多块场景：12003 字（段落切在 6001）→ 尾块不得小于 32 字
    text2 = "超" * 6000 + "\n" + "超" * 7001
    chunks2 = scorecard.build_review_chunks(text2, None, max_segments=4)
    assert len(chunks2) == 3, f"应切为 3 块（1 字碎块并入前块），实际 {len(chunks2)}"
    assert all(len(c) >= 32 for c in chunks2), "不允许多块方案里残留碎块（每块都值一次 map 调用）"
    assert sum(len(c) for c in chunks2) == len(text2), "切块不得丢字"


def test_map_reserves_last_budget_credit_for_reduce():
    """门禁 P3 整改：预算只够 map 吃时必须给 reduce 留 1 次额度，评分卡不得恒 unavailable。"""
    text = _fixture_text()
    chat, calls = _chat_router([_reduce_payload()], map_output=_map_observations())
    result = _run(text, chat, budget=ReviewBudget(2))
    # 预算 2：map 1 次（剩 1 时预留停手）+ reduce 1 次
    assert len(calls) == 2
    assert result["scorecard"]["available"] is True, "预留额度必须保证 reduce 能跑"


def test_format_observations_scrubs_candidate_quote(monkeypatch):
    """肉饼门禁 P3-1：禁语不得借候选 quote 字段回流 reduce prompt。"""
    block = scorecard.format_observations(
        [{
            "segment": "C",
            "comment": "正常评语",
            "gap_item_ids": [],
            "candidates": [{"item_id": "x", "name": "y", "note": "n", "quote": "这份合同没有问题"}],
        }]
    )
    assert "没有问题" not in block
    assert "【已过滤】" in block
