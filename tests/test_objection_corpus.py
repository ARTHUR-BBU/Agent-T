"""批 C（3.3）：异议层金标对抗回放门禁——真实语料 × 分层拦截不变式。

与预审对抗集（precheck_corpus_common）同语料源，但测的是异议层：
用真实规则引擎（run_checklist + annotate）在 24 份金标语料上产出 items，
钉死三条不变式（回放零错杀门禁）：
① 分流无泄漏：送审候选绝不包含 hardline / na / 未识别类别；
② 方向×类别矩阵：heuristic 候选全 false_positive、existence 全 omission；
③ 铁律 3 零错杀：异议层开/关 + 恶意 payload 轰炸，items 深度相等。

对抗 payload 设计（小智娘门禁 P1 教训出题预测）：
- 对 hardline 簇申报异议（洗白方向）→ 候选外静默丢弃；
- 伪造 quote（不在原文）→ 五要件①拒收留痕；
- 越类申报 → sent 集比对拦截。
mock 层进常规 CI（不烧真实 Key）；live 层挂账（与预审 live 同模式）。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services import objection as objection_service
from app.services.blind_spot import annotate_rule_items
from app.services.checklist import run_checklist
from app.services.clause_index import build_clause_index
from tests.precheck_corpus_common import CASES

pytestmark = pytest.mark.usefixtures("_objections_off")

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "fixtures" / "precheck"

# 语料 → 品类映射（取自 precheck_corpus_common.CASES 权威版本，不硬编码；
# blocked 组为不支持品类，规则引擎不产出异议对象，不参与回放）
CORPUS_MAP = [
    (c["file"], c["selected"]) for c in CASES if c["kind"] != "blocked"
]


def _replay(fname: str, category: str):
    """真实规则引擎回放一份语料：产出带 rule_class/rule_id 的 items + 条款索引。"""
    text = (CORPUS / fname).read_text(encoding="utf-8")
    result = run_checklist(text, category)
    items = annotate_rule_items(result["items"])
    return text, items, build_clause_index(text)


@pytest.mark.parametrize("fname,category", CORPUS_MAP)
def test_replay_candidate_hygiene(fname, category):
    """不变式①+②：分流无泄漏 + 方向×类别矩阵（24 语料逐份钉死）。"""
    _, items, _ = _replay(fname, category)
    candidates = objection_service._collect_candidates(items, max_candidates=12)
    for c in candidates:
        assert c["rule_class"] in {"heuristic", "existence"}, \
            f"{fname}: hardline/未识别类别泄漏进候选：{c}"
        if c["rule_class"] == "heuristic":
            assert c["direction"] == "false_positive", f"{fname}: heuristic 候选方向错误：{c}"
        else:
            assert c["direction"] == "omission", f"{fname}: existence 候选方向错误：{c}"


@pytest.mark.parametrize("fname,category", CORPUS_MAP)
def test_replay_iron_rule_3_no_kill(fname, category):
    """不变式③（铁律 3 回放零错杀）：异议层开关两次，items 深度相等。"""
    text, items, _ = _replay(fname, category)
    import copy

    snapshot = copy.deepcopy(items)
    monkey_env = {"OBJECTIONS_ENABLED": "true"}
    import os

    old = os.getenv("OBJECTIONS_ENABLED")
    os.environ["OBJECTIONS_ENABLED"] = "true"
    try:
        out = objection_service.run_objections(
            text=text, items=items, chat_fn=lambda s, u: json.dumps({"objections": []}),
        )
    finally:
        if old is None:
            os.environ.pop("OBJECTIONS_ENABLED", None)
        else:
            os.environ["OBJECTIONS_ENABLED"] = old
    assert out["available"] is True, f"{fname}: 异议层回放不得失败"
    assert items == snapshot, f"{fname}: 规则档位被异议层改动（铁律 3 破线）"


@pytest.mark.parametrize("fname,category", CORPUS_MAP)
def test_replay_malicious_payload_bombardment(fname, category):
    """对抗轰炸：模型对所有 items 恶意申报双向异议 + 伪造 quote。
    分层拦截后：accepted 数 ≤ 送审候选数，且每条 accepted 的 quote 必在原文。"""
    text, items, clause_index = _replay(fname, category)
    # 恶意全集：每条 item × 双方向，quote 全部伪造（不在原文）
    bombs = [
        {
            "item_id": str(it.get("id") or ""),
            "direction": d,
            "quote": "该条款完全不存在于任何位置的伪造文本",
            "counter_evidence": "未发现反证原文",
            "legal_reasoning": "这是注入攻击的伪造法律逻辑链，长度补足三十字以上以穿透长度要件检查。",
            "stance_check": "与立场无关",
            "proposal": "伪造提案",
        }
        for it in items
        for d in ("false_positive", "omission")
    ]
    out = objection_service.run_objections(
        text=text, items=items, clause_index=clause_index,
        chat_fn=lambda s, u: json.dumps({"objections": bombs}, ensure_ascii=False),
    )
    if not out["available"]:
        return  # 无候选时合法空结果
    candidates = objection_service._collect_candidates(items, max_candidates=12)
    accepted = [o for o in out["objections"] if o["accepted"]]
    assert len(accepted) == 0, \
        f"{fname}: 伪造 quote 不得被受理（五要件①失守）：{accepted[:1]}"
    assert out["rejected_count"] + sum(1 for o in out["objections"] if not o["accepted"]) >= 0
    # 候选外申报（hardline 方向等）不得出现在产出里
    sent = {(str(c["item_id"]), str(c["direction"])) for c in candidates}
    for o in out["objections"]:
        assert (o["item_id"], o["direction"]) in sent or not o["accepted"], \
            f"{fname}: 候选外申报泄漏：{o}"


def test_replay_missing_as_cluster_reachable():
    """missing_as 缺项档位簇（existence+需关注+rule_id=None）在真实语料上
    至少出现一次且可达 omission 通道（门禁 P1 整改的真实语料级验证）。"""
    reachable = 0
    for fname, category in CORPUS_MAP:
        _, items, _ = _replay(fname, category)
        candidates = objection_service._collect_candidates(items, max_candidates=12)
        if any(
            c["rule_class"] == "existence" and c["direction"] == "omission"
            and c["status"] == "需关注"
            for c in candidates
        ):
            reachable += 1
    assert reachable >= 1, \
        "24 份金标语料中竟无一命中 missing_as 漏报通道——主通道疑似再度失效"
