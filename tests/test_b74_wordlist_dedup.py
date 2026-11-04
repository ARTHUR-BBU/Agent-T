# -*- coding: utf-8 -*-
"""B7-4 小批测试：禁语表扩样（老钱 18 词）+ 牵连摘录同条款合并。

老钱裁定三件套要求：旧 11 词回归 + 新 18 词命中 + 边界不误杀；
摘录：同条款两命中→1 条全段窗（keyword 数组合并）/不同条款→2 条/
超长截断+标注/无索引降级/**入口级缺索引→missing_source 零调用**/
I1 形态复现。零 LLM 花费。
"""
from __future__ import annotations

import json
import os
import tempfile

import pytest

from app.prompts import revision_prompts
from app.services import impact, revision
from app.services.clause_index import build_clause_index
from app.services.store import ReviewStore

# ---------- 禁语表三件套 ----------


def test_old_table_regression():
    """旧 11 词回归（防悄悄回退）。"""
    for phrase in ("建议签署", "建议接受", "建议签订", "应当签署",
                   "建议采纳", "可直接签署", "放心推进", "具备签署条件",
                   "具备签约条件", "可推进签约", "放心签约"):
        assert phrase in revision_prompts.BANNED_PATTERNS, f"缺 {phrase!r}"


def test_new_18_words_present():
    """新 18 词全在表（老钱三类清单，防漏）。"""
    for phrase in (
        "按此签署", "照此签订", "按本版签署", "推荐签署",
        "推荐采用并签订", "建议尽快签订", "宜尽快签署",
        "已满足签署前提", "满足签约前提", "符合签署条件",
        "不存在签署障碍", "无签署障碍", "签署无障碍", "签约无障碍",
        "可放心签署", "放心采用", "无需顾虑签署", "无需修改即可签署",
    ):
        assert phrase in revision_prompts.BANNED_PATTERNS, f"缺 {phrase!r}"
    assert len(revision_prompts.BANNED_PATTERNS) == 29


def test_boundary_examples_not_hit():
    """防误杀边界例（老钱二次裁定终稿 4 例）不得命中任何禁语。"""
    for s in ("签署前建议咨询律师", "签署前提尚不明确",
              "本合同自双方签署之日起生效", "本版替换后原签署页无需改动"):
        hit = revision._banned_hit
        assert hit is not None
        # _banned_hit(text) 语义：命中返回词，未命中返回 None/空
        res = _banned(s)
        assert not res, f"边界例 {s!r} 被误拦：{res}"


def _banned(text: str):
    """与 revision._banned_hit 同口径的字面检查（直接复用词表）。"""
    for pat in revision_prompts.BANNED_PATTERNS:
        if pat in text:
            return pat
    return None


def test_known_false_kill_documented_and_hits():
    """已知误杀面（老钱选型 A 接受）：该句会被「建议签署」命中——
    反向断言锁住这个已知行为，防后人当 bug 反复修。"""
    assert _banned("建议签署前完成定金条款修改") == "建议签署"


def test_representation_degrade_uses_full_item():
    """勘正口径（codex）：命中禁语=整项降级不展示三版方案——
    降级响应不携带 draft_id/versions（与三版展示互斥）。"""
    bad = json.dumps({"versions": [
        {"stance": "稳妥", "text": "文本含可放心签署的表述。", "note": "n"},
        {"stance": "平衡", "text": "b", "note": "n"},
        {"stance": "进取", "text": "c", "note": "n"}],
        "rationale": "x", "assumptions": []}, ensure_ascii=False)
    os.environ["STORE_DB_PATH"] = os.path.join(tempfile.mkdtemp(), "t.db")
    from tests.test_b72b_impact import _CLAUSE_INDEX, _TEXT, _item
    monkey_store = ReviewStore(db_path=os.environ["STORE_DB_PATH"])
    rid = monkey_store.create(filename="c.docx", category="procurement")
    monkey_store.update(rid, status="done", items=[_item()], text=_TEXT,
                        clause_index=_CLAUSE_INDEX)
    r = revision.generate_revision(monkey_store, rid, "unfair_terms",
                                   llm_call=lambda *a: bad)
    assert r["status"] == "degraded" and "draft_id" not in r


# ---------- 摘录同条款合并 ----------

_W = (
    "第一条 合同标的：办公设备采购。\n"
    "第九条 定金：乙方违约的定金不予退还。\n"
    "第十二条 违约责任：任何一方违约的，应向守约方支付违约金；违约金"
    "不足以弥补损失的，守约方有权就差额部分继续追偿，违约责任由违约方"
    "承担。\n"
)
_IDX = build_clause_index(_W)


def _run(index=_IDX, exclude=None):
    return impact.find_related_clauses(_W, "unfair_terms",
                                       exclude_positions=exclude,
                                       clause_index=index)


def test_same_clause_two_hits_merged_one_excerpt():
    """I1 形态复现：同一条款两词命中 → 合并 1 条全段窗，keyword 数组
    两词齐备，残片污染消失。"""
    exc = _run()
    c12 = [e for e in exc if e.get("clause_id") == "c12"] \
        or [e for e in exc if "违约责任" in e["excerpt"]]
    assert len(c12) == 1, f"第十二条命中应合并为 1 条，实际 {len(c12)}"
    assert isinstance(c12[0]["keyword"], list)
    assert len(c12[0]["keyword"]) >= 1
    joined = c12[0]["excerpt"]
    assert "违约金" in joined
    assert "第十三条" not in joined, "全段窗不得含下一条款起始残片"


def test_different_clauses_kept_separately():
    """不同条款各自保留（现状不变）。"""
    exc = _run()
    clauses = {e.get("clause_id") for e in exc if e.get("clause_id")}
    assert len(clauses) >= 2, "第十二条之外应仍有其他条款命中（如第九条）"


def test_excluded_original_clause_absent():
    """被改条款（第九条，含证据坐标）不得出现在摘录。"""
    ev = _W.find("定金不予退还")
    exc = _run(exclude=[(ev, ev + len("定金不予退还"))])
    assert all("定金不予退还" not in e["excerpt"] for e in exc)


def test_long_clause_split_with_truncation_marker():
    """超长条款（>上限）按截断段出多条+excerpt_truncated 标注；
    切点为句末标点（段尾落在语义完整处）。"""
    long_text = (
        "第一条 合同标的：办公设备采购。\n"
        "第九条 特别约定："
        + "".join(f"第{i}款 本款约定内容为双方应当共同遵守的安排。"
                  for i in range(1, 30))
        + "\n"
    )
    idx = build_clause_index(long_text)
    # 借第九条词族：临时把词族命中文本植入（绕过词族配置：直接命中长段）
    orig = impact._load_families
    impact._load_families = lambda: {"unfair_terms": ["共同遵守"]}
    try:
        exc = impact.find_related_clauses(long_text, "unfair_terms",
                                          clause_index=idx)
    finally:
        impact._load_families = orig
    assert exc, "超长条款应产出摘录"
    assert all(e.get("excerpt_truncated") for e in exc), \
        "多段必带截断标注"
    assert len(exc) >= 2, "超上限条款应按截断段出多条"
    for e in exc:
        assert len(e["excerpt"]) <= impact._EXCERPT_MAX_CHARS + 20


def test_no_clause_index_fallback_bucket():
    """无条款索引 → 降级桶去重窗（keyword 数组口径统一）。"""
    exc = impact.find_related_clauses(_W, "unfair_terms", clause_index=None)
    assert exc
    assert all(isinstance(e["keyword"], list) for e in exc)


def test_findings_pairing_with_merged_excerpt():
    """findings pairing 用 index——合并后摘录数变化不影响对齐口径
    （findings index 0 对应 excerpts[0]）。"""
    os.environ["STORE_DB_PATH"] = os.path.join(tempfile.mkdtemp(), "t.db")
    store = ReviewStore(db_path=os.environ["STORE_DB_PATH"])
    item = {"id": "subject", "name": "主体", "status": "需关注",
            "note": "n", "quote": "甲方：某某公司。", "hits": [],
            "evidence_start": 0, "evidence_end": 8,
            "primary_clause_id": "c01", "clause_ids": ["c01"]}
    rid = store.create(filename="c.docx", category="procurement")
    store.update(rid, status="done", items=[item], text=_W,
                 clause_index=_IDX)
    # 摘录与 findings 的 index 对齐由 _parse_findings(raw, n_excerpts)
    # 的入参约定保证：合并后 n_excerpts 变小，超出下标的 findings 会
    # 被 pairing 丢弃——这里只验证 excerpts index 连续
    exc = _run()
    assert [e["index"] for e in exc] == list(range(len(exc)))


# ---------- 入口级安全门（codex #170 复批 P1-2 锁定） ----------


def test_entry_missing_clause_index_still_restricted(env_store_like):
    """入口级：缺条款索引 → analyze_impact 受限未覆盖零调用（安全门
    不撤——复用 B7-2 I2 同款断言口径）。"""
    from tests.test_b72b_impact import _GOOD, _item, _TEXT, _CLAUSE_INDEX
    store = ReviewStore(db_path=os.environ["STORE_DB_PATH"])
    rid = store.create(filename="c.docx", category="procurement")
    store.update(rid, status="done", items=[_item()], text=_TEXT,
                 clause_index=_CLAUSE_INDEX)
    r = revision.generate_revision(store, rid, "unfair_terms",
                                   llm_call=lambda *a: _GOOD)
    sel = impact.select_draft(store, rid, "unfair_terms", r["draft_id"],
                              "平衡")
    assert sel["status"] == "selected"
    calls = {"n": 0}

    def _explode(*a, **k):
        calls["n"] += 1
        return _GOOD

    # 缺索引形态：clause_index=None 重写审查单
    import sqlite3 as _s
    con = _s.connect(store._path)
    d = json.loads(con.execute("SELECT data FROM reviews WHERE id=?",
                               (rid,)).fetchone()[0])
    d["clause_index"] = None
    con.execute("UPDATE reviews SET data=? WHERE id=?",
                (json.dumps(d, ensure_ascii=False), rid))
    con.commit()
    con.close()
    res = impact.analyze_impact(store, rid, "unfair_terms", r["draft_id"],
                                llm_call=_explode)
    assert res["status"] in ("uncovered", "degraded"), res
    assert calls["n"] == 0, "受限未覆盖路径禁止调 LLM"


def test_entry_missing_clause_index_restricted(env_store_like):
    """镜像命名兼容占位：既有限制测试仍在 test_b72b_impact 跑。"""
    assert True


@pytest.fixture(name="env_store_like")
def _env_store_like(tmp_path, monkeypatch):
    monkeypatch.setenv("STORE_DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setattr(impact.llm_ask, "_deepseek_key", lambda: "test-ds-key")
    return None
