"""Basic API smoke tests (no Grok key required)."""
from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app
from tests.helpers import wait_review_done

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "fixtures" / "procurement_sample.txt"

client = TestClient(app)


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_upload_and_review():
    files = {"file": ("procurement_sample.txt", FIXTURE.read_bytes(), "text/plain")}
    data = {"category": "procurement"}
    r = client.post("/api/upload", files=files, data=data)
    assert r.status_code == 200
    rid = r.json()["review_id"]

    # 审查已改后台任务：轮询到完成
    body = wait_review_done(client, rid)
    assert body["status"] == "done"
    attention = [i for i in body["items"] if i["status"] == "需关注"]
    assert len(attention) >= 7


def test_review_has_scorecard_unavailable_without_key(monkeypatch):
    """M3.5：无 Key 时评分卡字段存在且明确未开通，规则结果照常."""
    for k in ("ZHIPU_API_KEY", "GLM_API_KEY", "XAI_API_KEY", "GROK_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    files = {"file": ("procurement_sample.txt", FIXTURE.read_bytes(), "text/plain")}
    rid = client.post("/api/upload", files=files, data={"category": "procurement"}).json()[
        "review_id"
    ]
    body = wait_review_done(client, rid)
    sc = body.get("scorecard") or {}
    assert sc.get("available") is False
    assert sc.get("reason") == "no_llm_key"
    assert sc.get("advisory_only") is True
    # 规则结果不受影响
    attention = [i for i in body["items"] if i["status"] == "需关注"]
    assert len(attention) >= 7


def test_review_with_mocked_llm_exposes_full_scorecard_structure(monkeypatch):
    """M3.5：有 Key（mock 模型通道）时 /api/review 的 scorecard 结构必须完整，
    补盲候选走 blind 通道，规则条目档位与 tag_source 原样透传."""
    from app.graph import pipeline

    monkeypatch.setenv("BLIND_SPOT_ENABLED", "true")
    scorecard_final = {
        "available": True,
        "reason": None,
        "total": 72,
        "tier": {"label": "有实质风险", "hint": "先改完再谈签的事"},
        "summary": "规则结果汇总评分 72 分。",
        "segments": [
            {"key": k, "name": f"段{k}", "weight": w, "score": w - 1, "comment": "", "na": False}
            for k, w in [("A", 12), ("B", 24), ("C", 14), ("D", 16), ("E", 14), ("F", 10), ("G", 10)]
        ],
        "caps_applied": ["因存在【标的】需关注项，总分已按上限 74 封顶（失分不能互相抵扣）"],
        "disclaimer": "以上都是机器给的参考意见，签之前建议找懂行的人再看一眼。",
        "advisory_only": True,
    }

    def fake_review(**kwargs):
        return {
            "scorecard": scorecard_final,
            "blind_candidates": [
                {
                    "id": "term", "name": "期限", "status": "需关注",
                    "note": "缺少履约期限", "quote": "签订后 20 日内交付",
                    "hits": [], "tag_source": "blind", "needs_confirm": True,
                    "named_by_scorecard": False,
                }
            ],
            "blind_skipped_messages": [],
            "blind_skipped_reason": None,
            "blind_enabled": True,
        }

    monkeypatch.setattr(pipeline, "run_model_review", fake_review)

    files = {"file": ("procurement_sample.txt", FIXTURE.read_bytes(), "text/plain")}
    rid = client.post("/api/upload", files=files, data={"category": "procurement"}).json()[
        "review_id"
    ]
    body = wait_review_done(client, rid)
    assert body["status"] == "done"

    sc = body["scorecard"]
    assert sc["available"] is True
    assert sc["total"] == 72
    assert sc["tier"]["label"] == "有实质风险"
    assert len(sc["segments"]) == 7
    assert all({"key", "name", "weight", "score", "comment", "na"} <= set(s) for s in sc["segments"])
    assert sc["caps_applied"] and "74" in sc["caps_applied"][0]
    assert sc["advisory_only"] is True
    assert sc["disclaimer"]

    # 补盲候选走独立通道，needs_confirm 必须为真
    assert len(body["blind_candidates"]) == 1
    cand = body["blind_candidates"][0]
    assert cand["tag_source"] == "blind"
    assert cand["needs_confirm"] is True
    assert cand["quote"]

    # 规则条目不受评分卡影响：需关注条目 tag_source 仍是 rule
    attention = [i for i in body["items"] if i["status"] == "需关注"]
    assert len(attention) >= 7
    assert all(i.get("tag_source") == "rule" for i in attention)
    assert body["blind_enabled"] is True
    assert body["blind_skipped_reason"] is None


def test_ask_without_key_clear_message(monkeypatch):
    for k in ("ZHIPU_API_KEY", "GLM_API_KEY", "XAI_API_KEY", "GROK_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    files = {"file": ("procurement_sample.txt", FIXTURE.read_bytes(), "text/plain")}
    rid = client.post("/api/upload", files=files, data={"category": "procurement"}).json()[
        "review_id"
    ]
    review = wait_review_done(client, rid)
    assert review.get("ask_available") is False
    item = next(i for i in review["items"] if i["status"] == "需关注")
    r = client.post(
        "/api/ask",
        json={"review_id": rid, "item_id": item["id"], "question": "风险大吗？"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body.get("ok") is False
    assert (body.get("error") or "") == "追问暂未开通"
