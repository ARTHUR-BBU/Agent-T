"""异议层（阶段 3.1/3.2）API 集成测试：数据流 + 铁律 3 硬断言 + adopt 端点。

铁律 3 硬断言（验收口径）：同一份合同，异议层开/关两次审查，items 与
scorecard 必须**深度相等**——异议是候选线索不是裁判，结构上不可能影响
规则档位与评分（同 quality 铁律 5 先例）。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.store import store as store_module
from tests.helpers import wait_review_done

client = TestClient(app)

SAMPLE_OBJECTION = {
    "item_id": "penalty_cap",
    "rule_id": "penalty_cap#r0",
    "rule_class": "heuristic",
    "direction": "false_positive",
    "quote": "第三条 乙方应于收货后七日内提出书面异议。",
    "counter_evidence": "未发现反证原文",
    "legal_reasoning": "异议期七日约定属于常见商务条款，规则词表对「书面异议」"
                       "的命中语境未区分质量异议期与付款异议期，疑似误报。",
    "stance_check": "与立场无关",
    "proposal": "为「异议期」词表增加质量异议期/付款异议期的语境区分。",
    "accepted": True,
    "reject_reason": None,
    "clause_id": "c03",
    "clause_ambiguous": False,
    "adopted": False,
    "needs_confirm": True,
}

SAMPLE_OBJECTIONS = {
    "available": True,
    "reason": None,
    "objections": [SAMPLE_OBJECTION],
    "rejected_count": 0,
    "disclaimer": "异议只是候选线索，不改变逐条核查结论；是否成立由人工与规则修订决定。",
}


@pytest.fixture
def objections_on(monkeypatch):
    monkeypatch.setenv("OBJECTIONS_ENABLED", "true")
    from app.graph import pipeline as pipeline_module
    from app.services import objection as objection_service

    monkeypatch.setattr(
        objection_service, "run_objections",
        lambda **kw: dict(SAMPLE_OBJECTIONS), raising=True,
    )
    yield
    pipeline_module.reset_graph()


def _upload_sample():
    from pathlib import Path

    sample = Path(__file__).resolve().parents[1] / "fixtures" / "procurement_sample.txt"
    files = {"file": ("procurement_sample.txt", sample.read_bytes(), "text/plain")}
    rid = client.post("/api/upload", files=files, data={"category": "procurement"}).json()["review_id"]
    return wait_review_done(client, rid)


def test_api_review_objections_full_chain(objections_on):
    """objections 全链透出：五要件字段/needs_confirm/disclaimer 结构完整，schema 校验通过。"""
    body = _upload_sample()
    assert body["status"] == "done"
    o = body["objections"]
    assert o is not None and o["available"] is True
    assert len(o["objections"]) == 1
    first = o["objections"][0]
    assert first["needs_confirm"] is True, "needs_confirm 必须代码强制"
    assert first["accepted"] is True
    assert first["clause_id"] == "c03"
    assert o["disclaimer"]
    # 铁律 3 展示语义：available=False 的异议数据不该出现在成品里（本用例 mock 成功路径）


def test_iron_rule_3_objections_cannot_change_items_or_scorecard(monkeypatch):
    """铁律 3 硬断言：异议层开/关两次审查，items+scorecard 深度相等。"""
    # 第一轮：异议层关闭（conftest 默认），拿到基线
    body_off = _upload_sample()
    assert body_off["status"] == "done"
    assert body_off["objections"]["available"] is False

    # 第二轮：开启并注入富异议产出
    monkeypatch.setenv("OBJECTIONS_ENABLED", "true")
    from app.services import objection as objection_service

    monkeypatch.setattr(
        objection_service, "run_objections",
        lambda **kw: dict(SAMPLE_OBJECTIONS), raising=True,
    )
    body_on = _upload_sample()

    assert body_on["objections"]["available"] is True
    assert body_on["items"] == body_off["items"], "规则档位必须逐字节不受异议层影响"
    assert body_on["scorecard"] == body_off["scorecard"], "评分卡必须逐字节不受异议层影响"
    assert body_on["blind_candidates"] == body_off["blind_candidates"]


def test_old_record_without_objections_key():
    """旧记录兼容：store 行无 objections 键 → GET 200 且 objections=None（前端静默隐藏）。"""
    rid = store_module.create(
        filename="old.txt", category="lease", status="done", stage="done",
        created_at="2026-09-12 10:00", items=[], scorecard={},
        blind_candidates=[], blind_skipped_messages=[], blind_skipped_reason=None,
        blind_enabled=False, text="", policies=[], error=None,
        # 故意不传 objections——模拟阶段 3.1 之前的旧记录
    )
    r = client.get(f"/api/review/{rid}")
    assert r.status_code == 200
    assert r.json()["objections"] is None


# ---------- adopt 端点（阶段 3.2） ----------

def _make_done_with_objections():
    """造一条 done 记录：规则档位 + 已受理异议，供 adopt 测试。"""
    items = [
        {"id": "penalty_cap", "name": "违约金上限", "status": "需关注"},
        {"id": "delivery", "name": "交付时间", "status": "通过"},
    ]
    rid = store_module.create(
        filename="c.txt", category="procurement", status="done", stage="done",
        created_at="2026-09-15 10:00", items=items, scorecard={},
        blind_candidates=[], blind_skipped_messages=[], blind_skipped_reason=None,
        blind_enabled=False, text="", policies=[], error=None,
        objections=dict(SAMPLE_OBJECTIONS),
    )
    return rid


def test_adopt_marks_adopted_key_only():
    """采纳只标 adopted 键：items 档位逐字节不动（铁律 3 的 API 侧防线）。"""
    rid = _make_done_with_objections()
    before = client.get(f"/api/review/{rid}").json()
    r = client.post(f"/api/review/{rid}/objections/adopt", json={"index": 0})
    assert r.status_code == 200
    body = r.json()
    assert body["objections"][0]["adopted"] is True
    # items 档位不变
    after = client.get(f"/api/review/{rid}").json()
    assert after["items"] == before["items"], "采纳异议不得改动规则档位"
    # 持久化：再 GET 仍是 adopted
    assert client.get(f"/api/review/{rid}").json()["objections"]["objections"][0]["adopted"] is True


def test_adopt_rejects_bad_index_and_missing_data():
    rid = _make_done_with_objections()
    # 序号越界
    assert client.post(f"/api/review/{rid}/objections/adopt", json={"index": 9}).status_code == 404
    # 非整数 / 浮点 / 布尔（lax 转换不开口）
    assert client.post(f"/api/review/{rid}/objections/adopt", json={"index": "x"}).status_code == 422
    assert client.post(f"/api/review/{rid}/objections/adopt", json={"index": 0.9}).status_code == 422
    assert client.post(f"/api/review/{rid}/objections/adopt", json={"index": True}).status_code == 422
    # 缺 index
    assert client.post(f"/api/review/{rid}/objections/adopt", json={}).status_code == 422


def test_adopt_rejects_unaccepted_objection():
    """五要件拒收条目（accepted=False 留痕）不可采纳：API 层 422。"""
    rid = store_module.create(
        filename="r.txt", category="procurement", status="done", stage="done",
        created_at="2026-09-15 10:00", items=[], scorecard={},
        blind_candidates=[], blind_skipped_messages=[], blind_skipped_reason=None,
        blind_enabled=False, text="", policies=[], error=None,
        objections={**SAMPLE_OBJECTIONS, "objections": [
            {**SAMPLE_OBJECTION, "accepted": False, "reject_reason": "要件①条款引用未能在原文核验"},
        ]},
    )
    r = client.post(f"/api/review/{rid}/objections/adopt", json={"index": 0})
    assert r.status_code == 422
    # 拒收留痕未被污染
    assert client.get(f"/api/review/{rid}").json()["objections"]["objections"][0]["adopted"] is False
    # 无异议数据的 done 记录
    rid2 = store_module.create(
        filename="d.txt", category="procurement", status="done", stage="done",
        created_at="2026-09-15 10:00", items=[], scorecard={},
        blind_candidates=[], blind_skipped_messages=[], blind_skipped_reason=None,
        blind_enabled=False, text="", policies=[], error=None,
        objections={"available": False, "reason": "disabled", "objections": [],
                    "rejected_count": 0, "disclaimer": "x"},
    )
    assert client.post(f"/api/review/{rid2}/objections/adopt", json={"index": 0}).status_code == 404
