"""Deterministic tests for M3.5 scorecard (mocked LLM, no key needed).

Covers 老王 gate matrix + 老钱 opinion书 enforcement:
- 有规则结果才出分；关补盲仍有分；无 Key 明确未开通
- caps/floors enforced in code, not model trust
- forbidden hit → retry once → degrade to score-only
- naming incomplete → degrade
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services import scorecard
from app.services.blind_spot import annotate_rule_items
from app.services.checklist import run_checklist
from app.services.model_review import run_model_review

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "fixtures" / "procurement_sample.txt"


def _rule_items():
    text = FIXTURE.read_text(encoding="utf-8")
    result = run_checklist(text, category="procurement")
    return text, annotate_rule_items(result["items"])


def _payload(raw_dict):
    return json.dumps(raw_dict, ensure_ascii=False)


def _model_payload(total, seg_scores, summary="汇总。", comments=None, candidates=None):
    segs = []
    for key, sc in seg_scores.items():
        c = {"key": key, "score": sc}
        if comments and key in comments:
            c["comment"] = comments[key]
        segs.append(c)
    return {
        "scorecard": {"summary": summary, "segments": segs},
        "candidates": candidates or [],
    }


# ---------- config ----------

def test_scorecard_config_loads_and_weights_sum_100():
    for category in ("procurement", "nda"):
        cfg = scorecard.load_scorecard_config(category)
        segs = cfg["segments"]
        assert len(segs) == 7
        assert {s["key"] for s in segs} == {"A", "B", "C", "D", "E", "F", "G"}
        assert sum(s["weight"] for s in segs) == 100
    nda = {s["key"]: s for s in scorecard.load_scorecard_config("nda")["segments"]}
    assert nda["C"]["na"] is True and nda["C"]["weight"] == 0


def test_forbidden_list_loads():
    fb = scorecard.load_forbidden()
    assert fb, "forbidden config must not be empty"
    all_words = [w for words in fb.values() for w in words]
    assert "本合同没有问题" in all_words
    assert "可以放心签署" in all_words


def test_check_and_scrub_forbidden():
    hits = scorecard.check_forbidden("总体看本合同没有问题，可以放心签署。")
    assert "本合同没有问题" in hits
    scrubbed = scorecard.scrub_forbidden("本合同没有问题")
    assert "本合同没有问题" not in scrubbed


# ---------- postprocess: floors & caps (code-enforced, 老钱第三节) ----------

def test_cap_core_attention_limits_total():
    _, items = _rule_items()
    segments = scorecard.load_scorecard_config("procurement")["segments"]
    # model tries to give a shiny 95 despite B/D 需关注 items in gold fixture
    payload = _model_payload(
        95, {s["key"]: s["weight"] for s in segments},
        summary="整体良好。", comments={"B": "需关注项已说明：标的。", "D": "需关注项已说明：违约责任。"},
    )
    final = scorecard.postprocess(payload, items, segments)
    assert final["total"] <= 74
    # floors pulled B/D segment scores below weight
    by_key = {s["key"]: s for s in final["segments"]}
    assert by_key["B"]["score"] < by_key["B"]["weight"]
    assert by_key["D"]["score"] < by_key["D"]["weight"]


def test_cap_applied_message_when_floors_not_enough():
    """非核心段需关注：下限压不完，封顶 89 必须实际生效并留消息."""
    items = [
        {"id": "a", "name": "主体", "segment": "A", "status": "需关注", "category_na": False},
        {"id": "b", "name": "标的", "segment": "B", "status": "通过", "category_na": False},
    ]
    segments = scorecard.load_scorecard_config("procurement")["segments"]
    payload = _model_payload(
        100, {s["key"]: s["weight"] for s in segments},
        summary="整体良好。", comments={"A": "需关注项已说明：主体。"},
    )
    final = scorecard.postprocess(payload, items, segments)
    assert final["total"] <= 89
    assert final["caps_applied"], "cap 89 should actually truncate and leave a message"


def test_total_recomputed_from_segments_not_model():
    _, items = _rule_items()
    segments = scorecard.load_scorecard_config("procurement")["segments"]
    payload = _model_payload(
        95, {s["key"]: s["weight"] for s in segments}, summary="无问题也不信模型总分"
    )
    final = scorecard.postprocess(payload, items, segments)
    assert final["total"] == sum(s["score"] for s in final["segments"])
    assert final["total"] < 95  # gold has 需关注 items → cannot reach model claim


def test_na_segment_zero_and_disclaimer_present():
    text = (
        ROOT / "fixtures" / "nda_public_template.txt"
    ).read_text(encoding="utf-8")
    result = run_checklist(text, category="nda")
    items = annotate_rule_items(result["items"])
    segments = scorecard.load_scorecard_config("nda")["segments"]
    payload = _model_payload(100, {s["key"]: s["weight"] for s in segments}, summary="范本。")
    final = scorecard.postprocess(payload, items, segments)
    c_seg = next(s for s in final["segments"] if s["key"] == "C")
    assert c_seg["na"] is True and c_seg["score"] == 0
    assert final["disclaimer"]
    assert final["advisory_only"] is True


def test_forbidden_scrubbed_in_comments_and_summary():
    _, items = _rule_items()
    segments = scorecard.load_scorecard_config("procurement")["segments"]
    payload = _model_payload(
        80,
        {s["key"]: s["weight"] for s in segments},
        summary="本合同没有问题。",
    )
    final = scorecard.postprocess(payload, items, segments)
    assert "本合同没有问题" not in final["summary"]


# ---------- gates & degradation via run_model_review ----------

def _chat_returning(raw_dict, calls):
    def chat(_system, _user):
        calls.append(_user)
        return _payload(raw_dict)

    return chat


def test_gate_no_rule_results(monkeypatch):
    monkeypatch.setenv("BLIND_SPOT_ENABLED", "true")
    calls: list = []
    out = run_model_review(
        text="x", items=[], policies=[], category="procurement",
        chat_fn=_chat_returning(_model_payload(90, {}), calls),
    )
    assert out["scorecard"]["available"] is False
    assert out["scorecard"]["reason"] == "no_rule_results"
    assert not calls, "LLM must not be called without rule results"


def test_forbidden_retry_then_degrade(monkeypatch):
    monkeypatch.setenv("BLIND_SPOT_ENABLED", "true")
    text, items = _rule_items()
    segments = scorecard.load_scorecard_config("procurement")["segments"]
    bad = _model_payload(
        80, {s["key"]: s["weight"] for s in segments},
        summary="整体看本合同没有问题。",
    )
    calls: list = []
    out = run_model_review(
        text=text, items=items, policies=[], category="procurement",
        chat_fn=_chat_returning(bad, calls),
    )
    assert len(calls) == 2, "must retry exactly once on forbidden hit"
    assert out["scorecard"]["available"] is True
    assert out["scorecard"].get("degraded") is True
    assert "本合同没有问题" not in out["scorecard"]["summary"]


def test_naming_incomplete_degrades(monkeypatch):
    monkeypatch.setenv("BLIND_SPOT_ENABLED", "true")
    text, items = _rule_items()
    segments = scorecard.load_scorecard_config("procurement")["segments"]
    # 模型写了评语但刻意不点名任何非「通过」项 → 降级为仅展示分数。
    # 注意：评语必须非空——空评语会被代码自动补点名（肉饼整改后的兜底），不算漏点名
    evasive = {s["key"]: "整体尚可，无特别提示。" for s in segments}
    payload = _model_payload(
        80, {s["key"]: s["weight"] for s in segments},
        summary="大致尚可。", comments=evasive,
    )
    calls: list = []
    out = run_model_review(
        text=text, items=items, policies=[], category="procurement",
        chat_fn=_chat_returning(payload, calls),
    )
    assert len(calls) == 1
    assert out["scorecard"].get("degraded") is True
    assert out["scorecard"]["summary"].startswith("规则结果汇总评分")


def test_empty_comment_autofills_names_so_not_degraded(monkeypatch):
    """肉饼整改后的兜底：模型交白卷（评语为空）时代码自动补点名，不触发降级."""
    monkeypatch.setenv("BLIND_SPOT_ENABLED", "true")
    text, items = _rule_items()
    segments = scorecard.load_scorecard_config("procurement")["segments"]
    payload = _model_payload(80, {s["key"]: s["weight"] for s in segments}, summary="大致尚可。")
    out = run_model_review(
        text=text, items=items, policies=[], category="procurement",
        chat_fn=_chat_returning(payload, []),
    )
    sc = out["scorecard"]
    assert sc["available"] is True
    assert sc.get("degraded", False) is False
    # 兜底评语必须真的点到了硬伤条款名
    blob = "".join(str(s.get("comment") or "") for s in sc["segments"])
    for it in items:
        if it["status"] in ("需关注", "未找到"):
            assert it["name"] in blob, f"兜底评语漏点名：{it['name']}"


def test_clean_pass_not_degraded(monkeypatch):
    monkeypatch.setenv("BLIND_SPOT_ENABLED", "true")
    text, items = _rule_items()
    segments = scorecard.load_scorecard_config("procurement")["segments"]
    # mention every non-通过 item name in comments
    names = [i["name"] for i in items if i["status"] != "通过" and not i.get("category_na")]
    comments = {"A": "、".join(names)}
    payload = _model_payload(80, {s["key"]: s["weight"] for s in segments}, summary="点名：" + "、".join(names), comments=comments)
    out = run_model_review(
        text=text, items=items, policies=[], category="procurement",
        chat_fn=_chat_returning(payload, []),
    )
    assert out["scorecard"]["available"] is True
    assert out["scorecard"].get("degraded", False) is False


# ---------- 漏报压力测试（老钱意见书：四连风险合同封顶 74） ----------

FOUR_RISK = [
    ROOT / "fixtures" / "procurement_adversarial_2.txt",
    ROOT / "fixtures" / "procurement_adversarial_3.txt",
    ROOT / "fixtures" / "procurement_paraphrase.txt",
]


def test_four_risk_contract_rules_flag_core_segments():
    """漏报守门：签约即付全款+免质保+签收即验收+免除乙方违约责任，
    规则引擎必须在核心段（B 标的/期限、D 违约）打出「需关注」。
    规则层漏报 → 评分卡封顶逻辑根本不会触发，此测试先钉死规则层。"""
    for path in FOUR_RISK:
        text = path.read_text(encoding="utf-8")
        items = annotate_rule_items(run_checklist(text, "procurement")["items"])
        by_id = {i["id"]: i for i in items}
        for iid in ("payment", "breach", "unfair_terms"):
            assert by_id[iid]["status"] == "需关注", f"{path.name}: {iid} 漏报"
        core_flagged = [
            i for i in items
            if i.get("segment") in ("B", "D") and i["status"] == "需关注"
        ]
        assert core_flagged, f"{path.name}: 核心段 B/D 无任何需关注，评分封顶 74 不会触发"


@pytest.mark.parametrize("path", FOUR_RISK, ids=lambda p: p.name)
def test_four_risk_contract_model_full_marks_still_capped_74(path, monkeypatch):
    """漏报压力测试：模型对四连风险合同打满分，代码封顶后 total ≤ 74。"""
    monkeypatch.setenv("BLIND_SPOT_ENABLED", "true")
    text = path.read_text(encoding="utf-8")
    items = annotate_rule_items(run_checklist(text, "procurement")["items"])
    segments = scorecard.load_scorecard_config("procurement")["segments"]
    payload = _model_payload(
        98, {s["key"]: s["weight"] for s in segments},
        summary="条款完备，可以放心签署。",
        comments={"A": "主体：" + "、".join(i["name"] for i in items if i["status"] != "通过")},
    )
    calls: list = []
    out = run_model_review(
        text=text, items=items, policies=[], category="procurement",
        chat_fn=_chat_returning(payload, calls),
    )
    sc = out["scorecard"]
    assert sc["available"] is True
    assert sc["total"] <= 74, f"{path.name}: 四连风险封顶 74 失效，实际 {sc['total']}"
    assert sc["total"] == sum(s["score"] for s in sc["segments"])
    by_key = {s["key"]: s for s in sc["segments"]}
    # 核心段存在需关注 → 该段分数必须低于满分（扣分下限生效）
    assert by_key["B"]["score"] < by_key["B"]["weight"]
    assert by_key["D"]["score"] < by_key["D"]["weight"]
    # 模型的整体性背书不允许原样回显
    assert "可以放心签署" not in sc["summary"]


def test_core_cap_message_names_items():
    """封顶 74 生效时，消息必须点名条款（渲染为【XX】）且写明上限 74."""
    items = [
        {"id": "sm", "name": "标的", "segment": "B", "status": "需关注", "category_na": False},
    ]
    segments = scorecard.load_scorecard_config("procurement")["segments"]
    payload = _model_payload(
        100, {s["key"]: s["weight"] for s in segments},
        summary="良好。", comments={"B": "需关注项已说明：标的。"},
    )
    final = scorecard.postprocess(payload, items, segments)
    assert final["total"] == 74
    assert final["caps_applied"], "封顶 74 必须实际截断并留消息"
    assert "【标的】" in final["caps_applied"][0]
    assert "74" in final["caps_applied"][0]


# ---------- postprocess 数值健壮性 ----------

def test_segment_score_invalid_values_clamped():
    """模型给非数值/越界分段分：非数值按 0 计（与缺失对齐，乱答不给满分），
    负数钳 0，超满分钳满分."""
    items = []
    segments = [
        {"key": "A", "name": "A", "weight": 10, "na": False},
        {"key": "B", "name": "B", "weight": 10, "na": False},
        {"key": "C", "name": "C", "weight": 10, "na": False},
    ]
    payload = _model_payload(
        30, {"A": "十", "B": -5, "C": 999}, summary=""
    )
    final = scorecard.postprocess(payload, items, segments)
    by_key = {s["key"]: s["score"] for s in final["segments"]}
    assert by_key["A"] == 0   # 非数值 → 0（沉默≠满分，乱答也不给满分）
    assert by_key["B"] == 0   # 负数 → 0
    assert by_key["C"] == 10  # 超满分 → 满分


def test_parse_payload_fenced_and_prose_variants():
    inner = '{"scorecard": {"summary": "s", "segments": [{"key": "A", "score": 5}]}, "candidates": []}'
    assert scorecard.parse_model_payload(f"```json\n{inner}\n```") is not None
    assert scorecard.parse_model_payload(f"审查结果如下：{inner} 请参考。") is not None
    # 缺 scorecard 键 → None
    assert scorecard.parse_model_payload('{"candidates": []}') is None
    assert scorecard.parse_model_payload("") is None


def test_retry_parse_failure_reports_unavailable(monkeypatch):
    """首次禁语命中 → 重试；重试返回不可解析 → 明确 unavailable(parse_failed)."""
    monkeypatch.setenv("BLIND_SPOT_ENABLED", "true")
    text, items = _rule_items()
    segments = scorecard.load_scorecard_config("procurement")["segments"]
    bad = _payload(_model_payload(
        80, {s["key"]: s["weight"] for s in segments}, summary="整体看本合同没有问题。",
    ))
    seq = [bad, "模型第二轮输出了一段不包含 JSON 的自由文本"]

    def chat(_system, _user):
        return seq.pop(0)

    out = run_model_review(
        text=text, items=items, policies=[], category="procurement", chat_fn=chat
    )
    assert out["scorecard"]["available"] is False
    assert out["scorecard"]["reason"] == "parse_failed"


# ---------- 配置一致性（yaml 禁语 ↔ 代码禁语） ----------

def test_forbidden_yaml_all_categories_wired():
    """禁语 yaml 五大类都要真的接进 check_forbidden，不能只是躺在配置里."""
    fb = scorecard.load_forbidden()
    for cat in ("category_a", "category_b", "category_c", "category_d", "category_e"):
        assert fb.get(cat), f"{cat} 缺失或为空"
    samples = {
        "category_a": "结论：条款完备无瑕疵。",
        "category_b": "该约定不具法律效力。",
        "category_c": "这一项规则标错了。",
        "category_d": "本评分可替代律师审查。",
        "category_e": "对方不会违约。",
    }
    for cat, sentence in samples.items():
        hits = scorecard.check_forbidden(sentence)
        assert hits, f"{cat} 样例句未被拦截"
        assert any(w in fb[cat] for w in hits)


def test_banned_echo_no_duplicates_and_rewrite_guards_present():
    """M3.5 改写稿防线：三个「改后即安全」禁语必须在 BANNED_ECHO 里，且无重复项."""
    from app.services import llm_ask

    assert len(llm_ask.BANNED_ECHO) == len(set(llm_ask.BANNED_ECHO)), "BANNED_ECHO 有重复项"
    for w in ("已无风险", "已合规", "改后即无"):
        assert w in llm_ask.BANNED_ECHO, f"改写稿禁语 {w} 缺失"
    scrubbed = llm_ask._scrub_banned_echo("改后已无风险，已无风险已合规。")
    # 安全不变式：任何禁语（含其子串形态）不得原样存活
    for banned in ("已无风险", "无风险", "已合规"):
        assert banned not in scrubbed, f"禁语 {banned} 未被过滤: {scrubbed}"
    # 最长优先已修复（原报告 P4 弱点）：复合禁语不再碎成「已【已过滤】」，折叠为单个标记
    assert scrubbed == "改后【已过滤】", f"应折叠为单个标记，实际: {scrubbed}"
    # 对照组：评分卡侧 scrub_forbidden 的紧邻重复折叠是生效的
    assert scorecard.scrub_forbidden("本合同没有问题本合同没有问题") == "【已过滤】"


# ---------- 旧品类兼容（缺 scorecard: 块） ----------

def test_legacy_category_without_scorecard_block_does_not_emit_zero_score():
    """旧品类缺 scorecard: 块 → unavailable(no_scorecard_config)，不出「0 分+风险很大」假分.
    （原 strict xfail，开发狗已修，转正为常规回归测试）"""
    items = [{"id": "x", "name": "主体", "segment": "A", "status": "需关注", "category_na": False}]
    payload = _model_payload(90, {"A": 12}, summary="很好。")
    final = scorecard.postprocess(payload, items, [])  # 旧品类：无分段配置
    assert final["available"] is False
    assert final["reason"] == "no_scorecard_config"
    assert final["total"] is None


def test_scorecard_no_mutation_of_rule_items(monkeypatch):
    monkeypatch.setenv("BLIND_SPOT_ENABLED", "true")
    text, items = _rule_items()
    before = {i["id"]: i["status"] for i in items}
    segments = scorecard.load_scorecard_config("procurement")["segments"]
    names = [i["name"] for i in items if i["status"] != "通过" and not i.get("category_na")]
    payload = _model_payload(80, {s["key"]: s["weight"] for s in segments}, summary="点名：" + "、".join(names), comments={"A": "、".join(names)})
    run_model_review(text=text, items=items, policies=[], category="procurement", chat_fn=_chat_returning(payload, []))
    after = {i["id"]: i["status"] for i in items}
    assert before == after, "scorecard/blind must never mutate rule statuses"
