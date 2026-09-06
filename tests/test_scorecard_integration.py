"""M3.5 评分卡集成测试（API 层 + 边界 + 双层禁语一致性）。

与小智娘测试计划对应、开发狗 test_scorecard.py 未覆盖的增量：
- API 层：monkeypatch 合并模型调用，验证 /api/review/{id} 的 scorecard 结构完整
- 漏报压力：缺争议解决条款的四连风险 fixture（fixtures/procurement_four_risk.txt）
- 边界：模型报总分 100 必须被弃用；全 NA 分段防炸；payload 多余字段容错
- 禁语一致性：scorecard_forbidden.yaml 与 llm_ask.BANNED_ECHO 双层防线取并集后无漏网

无网络、无真实 Key；LLM 一律用假 chat_fn / monkeypatch 替身。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.graph import pipeline as pipeline_module
from app.main import app
from app.services import llm_ask, scorecard
from app.services.blind_spot import annotate_rule_items
from app.services.checklist import run_checklist
from app.services.model_review import run_model_review

ROOT = Path(__file__).resolve().parents[1]
FOUR_RISK = ROOT / "fixtures" / "procurement_four_risk.txt"
SAMPLE = ROOT / "fixtures" / "procurement_sample.txt"

client = TestClient(app)


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


def _chat_returning(raw_dict, calls):
    def chat(_system, _user):
        calls.append(_user)
        return _payload(raw_dict)

    return chat


# ---------- API 层：scorecard 结构出参完整 ----------

def test_api_review_scorecard_structure_complete(monkeypatch):
    """合并模型调用返回完整 scorecard + 补盲候选时，/api/review/{id} 必须原样透出：
    total/tier/segments/caps_applied/disclaimer/advisory_only，候选带 named_by_scorecard."""
    fake = {
        "scorecard": {
            "available": True,
            "reason": None,
            "total": 66,
            "tier": {"label": "有实质风险", "hint": "核心条款有硬伤，先改完再谈签的事"},
            "summary": "规则结果汇总评分 66 分。",
            "segments": [
                {"key": "A", "name": "主体资格", "weight": 10, "score": 8,
                 "comment": "存在需关注项：主体", "na": False},
                {"key": "B", "name": "核心内容", "weight": 20, "score": 12,
                 "comment": "存在需关注项：标的", "na": False},
            ],
            "caps_applied": ["因存在【标的】需关注项，总分已按上限 74 封顶（失分不能互相抵扣）"],
            "disclaimer": scorecard.DISCLAIMER,
            "advisory_only": True,
        },
        "blind_candidates": [
            {
                "id": "signature", "name": "签署与印章", "status": "需关注",
                "note": "规则未标需关注，模型提出候选风险", "quote": "双方各执一份",
                "hits": [], "tag_source": "blind", "needs_confirm": True,
                "named_by_scorecard": True,
            }
        ],
        "blind_skipped_messages": ["保密: 缺少原文依据，已跳过"],
        "blind_skipped_reason": None,
        "blind_enabled": True,
    }
    monkeypatch.setattr(pipeline_module, "run_model_review", lambda **kw: dict(fake))

    files = {"file": ("procurement_sample.txt", SAMPLE.read_bytes(), "text/plain")}
    rid = client.post("/api/upload", files=files, data={"category": "procurement"}).json()[
        "review_id"
    ]
    body = client.get(f"/api/review/{rid}").json()
    assert body["status"] == "done"

    sc = body["scorecard"]
    assert sc["available"] is True
    assert sc["advisory_only"] is True, "评分卡必须是纯参考层"
    assert sc["total"] == 66
    assert sc["tier"]["label"] and sc["tier"]["hint"]
    assert sc["segments"], "分段列表不能为空"
    for seg in sc["segments"]:
        assert {"key", "name", "weight", "score", "comment", "na"} <= set(seg)
    assert sc["caps_applied"] and "74" in sc["caps_applied"][0]
    assert sc["disclaimer"], "免责句是 D 类反向必备项，不能丢"

    cand = body["blind_candidates"][0]
    assert cand["tag_source"] == "blind"
    assert cand["needs_confirm"] is True
    assert cand["named_by_scorecard"] is True, "评分卡点名的候选要带独立标记"
    assert body["blind_skipped_messages"] == ["保密: 缺少原文依据，已跳过"]
    assert body["blind_enabled"] is True

    # 规则结果不受评分卡影响：items 仍是规则档位
    assert body["items"], "规则条目必须照常返回"


def test_api_scorecard_keys_absent_in_llm_response_defaults_safe(monkeypatch):
    """合并调用只返回半截 scorecard（缺 tier/disclaimer），API 层 schema 兜底不炸."""
    fake = {
        "scorecard": {"available": True, "total": 80, "segments": []},
        "blind_candidates": [],
        "blind_skipped_messages": [],
        "blind_skipped_reason": None,
        "blind_enabled": True,
    }
    monkeypatch.setattr(pipeline_module, "run_model_review", lambda **kw: dict(fake))

    files = {"file": ("procurement_sample.txt", SAMPLE.read_bytes(), "text/plain")}
    rid = client.post("/api/upload", files=files, data={"category": "procurement"}).json()[
        "review_id"
    ]
    body = client.get(f"/api/review/{rid}").json()
    sc = body["scorecard"]
    assert sc["total"] == 80
    assert sc["tier"] is None  # ScorecardInfo 默认值兜底
    assert sc["advisory_only"] is True


# ---------- 漏报压力：缺争议解决条款的四连风险 ----------

def test_four_risk_missing_dispute_clause_rules_flag():
    """缺争议解决条款 fixture 的规则层独有断言（遗留项⑥去重后保留的增量）：
    封顶 74 的模型侧测试已并入 test_scorecard.py 的 FOUR_RISK 参数化列表。"""
    text = FOUR_RISK.read_text(encoding="utf-8")
    items = annotate_rule_items(run_checklist(text, "procurement")["items"])
    by_id = {i["id"]: i for i in items}
    for iid in ("payment", "breach", "unfair_terms"):
        assert by_id[iid]["status"] == "需关注", f"{iid} 漏报"
    assert by_id["jurisdiction"]["status"] == "未找到", "缺争议解决条款必须未找到"
    assert by_id["governing_law"]["status"] == "需关注"
    core_flagged = [
        i for i in items
        if i.get("segment") in ("B", "D") and i["status"] in ("需关注", "未找到")
    ]
    assert core_flagged, "B/D 段无任何靶点，评分封顶逻辑不会触发"


# ---------- 边界与健壮性 ----------

def test_all_segments_na_does_not_crash():
    """全部分段 NA（理论不该出现的配置错误）：不应抛异常，结构仍完整可用."""
    items = [{"id": "x", "name": "主体", "segment": "A", "status": "需关注", "category_na": False}]
    segments = [
        {"key": "A", "name": "A", "weight": 0, "na": True},
        {"key": "B", "name": "B", "weight": 0, "na": True},
    ]
    payload = _model_payload(90, {"A": 90, "B": 90}, summary="很好。")
    final = scorecard.postprocess(payload, items, segments)
    assert final["available"] is True
    assert all(s["na"] for s in final["segments"])
    assert final["total"] == 0
    assert final["tier"]["label"], "全 NA 也应给出展示档位，不能除零或 KeyError"
    assert final["advisory_only"] is True


def test_payload_extra_fields_tolerated():
    """模型 JSON 带多余字段（驼峰以外的噪音）：解析与后处理都应容错."""
    raw = json.dumps({
        "scorecard": {
            "summary": "整体尚可，存在需关注项。",
            "total": 88,
            "confidence": 0.9,
            "segments": [
                {"key": "A", "score": 9, "comment": "主体需留意", "weight": 10, "extra": True},
            ],
        },
        "candidates": [],
        "notes": "模型附言，与结构无关",
    }, ensure_ascii=False)
    payload = scorecard.parse_model_payload(raw)
    assert payload is not None
    items = [{"id": "a", "name": "主体", "segment": "A", "status": "通过", "category_na": False}]
    segments = [{"key": "A", "name": "A", "weight": 10, "na": False}]
    final = scorecard.postprocess(payload, items, segments)
    assert final["available"] is True
    by_key = {s["key"]: s["score"] for s in final["segments"]}
    assert by_key["A"] == 9, "多余字段不应影响正常分段分"
    assert final["total"] == 9, "payload 里模型自报 total=88 必须被弃用"


def test_parse_payload_accepts_camelcase_scorecard_key():
    """键名大小写兼容已修（原 strict xfail 记录的缺口）：ScoreCard 归一化为小写键."""
    parsed = scorecard.parse_model_payload(
        '{"ScoreCard": {"summary": "s", "segments": []}, "candidates": []}'
    )
    assert parsed is not None
    assert isinstance(parsed["scorecard"], dict)


def test_not_found_deducts_but_does_not_cap():
    """「未找到」扣分段下限（≥60% 权重）。本场景总分远低于封顶线，不出封顶消息；
    「未找到」触发 89/74 封顶的路径由 test_m35_audit_fixes.py 钉死（b13d15f 已落地）."""
    items = [
        {"id": "c", "name": "保密", "segment": "E", "status": "未找到", "category_na": False},
    ]
    segments = [
        {"key": "E", "name": "E", "weight": 10, "na": False},
        {"key": "G", "name": "G", "weight": 10, "na": False},
    ]
    payload = _model_payload(20, {"E": 10, "G": 10}, summary="尚可。")
    final = scorecard.postprocess(payload, items, segments)
    by_key = {s["key"]: s for s in final["segments"]}
    assert by_key["E"]["score"] == 4, "未找到 → 下限 = 权重 40%"
    assert not final["caps_applied"], "当前设计：纯未找到不封顶"


# ---------- 禁语双层一致性（scorecard yaml ↔ llm_ask BANNED_ECHO） ----------

def test_forbidden_two_layers_union_no_escape():
    """背书/安全断言样例句，必须被评分卡禁语或追问禁语至少一层拦住（纵深防御）."""
    sentences = {
        # scorecard_forbidden.yaml A 类
        "本合同没有问题": "yaml",
        "可以放心签署": "yaml",
        "不存在法律风险": "yaml",
        # llm_ask.BANNED_ECHO（含改写稿防线）
        "改完后已无风险": "echo",
        "这个条款没有风险": "echo",
        "本合同已合规": "echo",
        "可以盖章": "echo",
    }
    for sentence, layer in sentences.items():
        sc_hits = scorecard.check_forbidden(sentence)
        echo_scrubbed = llm_ask._scrub_banned_echo(sentence)
        caught = bool(sc_hits) or echo_scrubbed != sentence
        assert caught, f"样例句「{sentence}」（预期 {layer} 拦截）两层都放行了"


def test_forbidden_scrubbers_idempotent_and_marked():
    """两层清洗器各自的不变量：单条禁语清洗后必含过滤标记，且二次清洗幂等
    （防止重复执行把【已过滤】再拼出新的禁语子串）."""
    yaml_words = [w for words in scorecard.load_forbidden().values() for w in words]
    assert yaml_words, "评分卡禁语清单不能为空"
    for w in yaml_words:
        once = scorecard.scrub_forbidden(w)
        assert "【已过滤】" in once, f"评分卡清洗未标记: {w}"
        assert scorecard.scrub_forbidden(once) == once, f"评分卡清洗不幂等: {w}"
    for w in llm_ask.BANNED_ECHO:
        once = llm_ask._scrub_banned_echo(w)
        assert "【已过滤】" in once, f"追问清洗未标记: {w}"
        assert llm_ask._scrub_banned_echo(once) == once, f"追问清洗不幂等: {w}"
    # 过滤标记本身不得构成任何禁语的一部分（拼接污染检查）
    all_words = set(yaml_words) | set(llm_ask.BANNED_ECHO)
    for w in all_words:
        assert "【已过滤】" not in w, f"禁语清单里混入了过滤标记本身: {w}"
