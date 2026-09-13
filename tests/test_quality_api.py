"""质量层（阶段 2.1）API 集成测试：数据流 + 铁律 5 硬断言 + 旧记录兼容。

铁律 5 硬断言（验收口径）：同一份合同，质量层开/关两次审查，items 与
scorecard 必须**深度相等**——质量层是参谋不是裁判，结构上不可能影响
规则档位与评分。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.store import store as store_module
from tests.helpers import wait_review_done

client = TestClient(app)

SAMPLE_QUALITY = {
    "available": True,
    "reason": None,
    "observations": [
        {
            "dimension": "consistency",
            "title": "付款与验收矛盾",
            "quote": "货到验收合格后一次性支付",
            "clause_id": None,
            "comment": "未约定验收标准，建议附验收清单。",
            "needs_confirm": True,
        },
        {
            "dimension": "impact",
            "title": "异议期偏短",
            "quote": "第三条 乙方应于收货后七日内提出书面异议。",
            "clause_id": "c03",
            "comment": "七日异议期偏短，建议延长。",
            "needs_confirm": True,
        },
    ],
    "disclaimer": "以上为 AI 观察，不构成审查结论、不影响逐条核查结果；每条均需人工确认。",
    "dropped_count": 0,
    "coverage": None,
}


@pytest.fixture
def quality_on(monkeypatch):
    monkeypatch.setenv("QUALITY_ENABLED", "true")
    from app.graph import pipeline as pipeline_module
    from app.services import quality as quality_service

    monkeypatch.setattr(
        quality_service, "run_quality",
        lambda **kw: dict(SAMPLE_QUALITY), raising=True,
    )
    yield
    pipeline_module.reset_graph()


def _upload_sample():
    from pathlib import Path

    sample = Path(__file__).resolve().parents[1] / "fixtures" / "procurement_sample.txt"
    files = {"file": ("procurement_sample.txt", sample.read_bytes(), "text/plain")}
    rid = client.post("/api/upload", files=files, data={"category": "procurement"}).json()["review_id"]
    return wait_review_done(client, rid)


def test_api_review_quality_full_chain(quality_on):
    """quality 全链透出：观察/needs_confirm/disclaimer 结构完整，schema 校验通过。"""
    body = _upload_sample()
    assert body["status"] == "done"
    q = body["quality"]
    assert q is not None and q["available"] is True
    assert len(q["observations"]) == 2
    assert all(o["needs_confirm"] is True for o in q["observations"])
    assert q["disclaimer"]
    # dimension 输出固定序
    assert [o["dimension"] for o in q["observations"]] == ["consistency", "impact"]


def test_iron_rule_5_quality_cannot_change_items_or_scorecard(monkeypatch):
    """铁律 5 硬断言：质量层开/关两次审查，items+scorecard 深度相等。"""
    # 第一轮：质量层关闭（conftest 默认），拿到基线
    body_off = _upload_sample()
    assert body_off["status"] == "done"
    assert body_off["quality"]["available"] is False

    # 第二轮：开启并注入富观察产出
    monkeypatch.setenv("QUALITY_ENABLED", "true")
    from app.services import quality as quality_service

    monkeypatch.setattr(
        quality_service, "run_quality",
        lambda **kw: dict(SAMPLE_QUALITY), raising=True,
    )
    body_on = _upload_sample()

    assert body_on["quality"]["available"] is True
    assert body_on["items"] == body_off["items"], "规则档位必须逐字节不受质量层影响"
    assert body_on["scorecard"] == body_off["scorecard"], "评分卡必须逐字节不受质量层影响"
    assert body_on["blind_candidates"] == body_off["blind_candidates"]


def test_old_record_without_quality_key(monkeypatch):
    """旧记录兼容：store 行无 quality 键 → GET 200 且 quality=None（前端静默隐藏）。"""
    rid = store_module.create(
        filename="old.txt", category="lease", status="done", stage="done",
        created_at="2026-09-12 10:00", items=[], scorecard={},
        blind_candidates=[], blind_skipped_messages=[], blind_skipped_reason=None,
        blind_enabled=False, text="", policies=[], error=None,
        # 故意不传 quality——模拟阶段 2.1 之前的旧记录
    )
    r = client.get(f"/api/review/{rid}")
    assert r.status_code == 200
    assert r.json()["quality"] is None


def test_parse_error_quality_semantics(monkeypatch):
    """解析失败路径：quality 走 error 短路，语义不掩盖上游错误（status 仍 error）。"""
    monkeypatch.setenv("QUALITY_ENABLED", "true")
    from app.services import quality as quality_service

    monkeypatch.setattr(
        quality_service, "run_quality",
        lambda **kw: (_ for _ in ()).throw(AssertionError("解析失败不得进入质量层")),
        raising=True,
    )
    files = {"file": ("broken.pdf", b"%PDF-this-is-not-a-real-pdf\x00\x01\x02", "application/pdf")}
    rid = client.post("/api/upload", files=files, data={"category": "procurement"}).json()["review_id"]
    body = wait_review_done(client, rid)
    assert body["status"] == "error"
    q = body.get("quality")
    assert q is not None and q["available"] is False and q["reason"] == "error"
