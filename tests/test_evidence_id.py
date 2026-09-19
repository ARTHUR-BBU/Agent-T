"""evidence_id 归一化回归（第三轮审计四项建议之 3）。"""
from __future__ import annotations

from app.services.evidence import attach_evidence_to_item, build_evidence, evidence_id_for

_TEXT = "第三条 违约金为总额百分之三十。"


def test_verified_has_id_missing_does_not():
    """审计实验复核：missing 票据无证据资格不得带 ID。"""
    ok = build_evidence(text=_TEXT, quote="违约金为总额百分之三十", parse_source="rules")
    assert ok["verification"] == "verified" and ok["evidence_id"].startswith("ev-")
    bad = build_evidence(text=_TEXT, quote="这句话绝不在合同里", parse_source="rules")
    assert bad["verification"] == "missing"
    assert bad["evidence_id"] == "", "无证据资格的票据不得带 ID（审计 P1-2）"


def test_attach_backfills_id_for_legacy_ticket():
    """审计 P1-1：历史票据（无 ID）经 attach 补齐字段后必须获得 ID。"""
    item = {
        "id": "x", "quote": "违约金为总额百分之三十",
        "primary_clause_id": "c03",
        "evidence": {  # 旧票据：无 evidence_id
            "document_version": "", "quote": "违约金为总额百分之三十",
            "start": None, "end": None, "clause_id": None,
            "verification": "verified", "parse_source": "rules",
        },
    }
    out = attach_evidence_to_item(
        item, text=_TEXT, parse_source="rules", document_version="dv1",
    )
    ev = out["evidence"]
    assert ev["evidence_id"].startswith("ev-"), "历史票据必须归一化补 ID（审计 P1-1）"
    assert ev["document_version"] == "dv1" and ev["clause_id"] == "c03"


def test_id_recomputed_when_fields_backfilled():
    """审计建议 3：字段被补全后 ID 必须重算（旧 ID 不得残留）。"""
    item = {
        "id": "x", "quote": "违约金为总额百分之三十",
        "primary_clause_id": "c03",
        "evidence": {
            "document_version": "dv1", "quote": "违约金为总额百分之三十",
            "start": 4, "end": 15, "clause_id": None,
            "verification": "verified", "parse_source": "rules",
            "evidence_id": "ev-stale000001",  # 补全 clause_id 前的旧 ID
        },
    }
    out = attach_evidence_to_item(
        item, text=_TEXT, parse_source="rules", document_version="dv1",
    )
    ev = out["evidence"]
    assert ev["clause_id"] == "c03"
    assert ev["evidence_id"] != "ev-stale000001", "字段补全后旧 ID 必须重算"
    assert ev["evidence_id"] == evidence_id_for(ev), "ID 必须与最终票据内容一致"


def test_same_span_cross_layer_same_id():
    """跨层同 span 同 ID（parse_source 不参与身份）。"""
    a = build_evidence(text=_TEXT, quote="违约金为总额百分之三十", parse_source="rules")
    b = build_evidence(text=_TEXT, quote="违约金为总额百分之三十", parse_source="objection")
    assert a["evidence_id"] == b["evidence_id"]


def test_read_path_normalizes_legacy_tickets():
    """审计 P2 读路径：get_review 对历史票据归一化——非资格清 ID、
    缺坐标 verified 补定位重算（STORE_TTL=0 也不得永续暴露）。"""
    from pathlib import Path

    from fastapi.testclient import TestClient

    from app.main import app
    from app.services.store import store as store_module

    client = TestClient(app)
    sample = Path(__file__).resolve().parents[1] / "fixtures" / "procurement_sample.txt"
    files = {"file": ("p.txt", sample.read_bytes(), "text/plain")}
    rid = client.post("/api/upload", files=files, data={"category": "procurement"}).json()["review_id"]
    from tests.helpers import wait_review_done
    wait_review_done(client, rid)

    row = store_module.get(rid)
    assert row is not None
    items = row.get("items") or []
    with_ev = [i for i in items if isinstance(i.get("evidence"), dict)]
    assert with_ev, "fixture 应有带票据的命中"
    # 造两张历史票据：①verified 缺坐标 ②missing 带旧误发 ID
    with_ev[0]["evidence"].pop("start", None)
    with_ev[0]["evidence"].pop("end", None)
    with_ev[0]["evidence"].pop("evidence_id", None)
    if len(with_ev) > 1:
        with_ev[1]["evidence"]["verification"] = "missing"
        with_ev[1]["evidence"]["evidence_id"] = "ev-stalefromoldcode"
    store_module.update(rid, items=items)

    body = client.get(f"/api/review/{rid}").json()
    out_items = body["items"]
    e0 = next(i for i in out_items if i["id"] == with_ev[0]["id"])["evidence"]
    assert e0["evidence_id"].startswith("ev-"), "缺坐标 verified 旧票必须补定位并重算 ID"
    assert e0["start"] is not None, "归一化必须补坐标"
    if len(with_ev) > 1:
        e1 = next(i for i in out_items if i["id"] == with_ev[1]["id"])["evidence"]
        assert e1["evidence_id"] == "", "非资格票据的旧误发 ID 必须清除"
