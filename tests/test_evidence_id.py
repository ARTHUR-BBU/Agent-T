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


def test_verified_unlocatable_downgrades_to_missing():
    """第三轮复核 P1-1 反例：verified 但摘句不在正文且无坐标——
    重新定位失败必须降级 missing、清坐标、不发 ID（实测曾发 ev-*）。"""
    item = {
        "id": "x", "quote": "这句话绝不在合同原文之中",
        "evidence": {
            "document_version": "dv", "quote": "这句话绝不在合同原文之中",
            "start": None, "end": None, "clause_id": None,
            "verification": "verified", "parse_source": "rules",
            "evidence_id": "ev-staleverified1",
        },
    }
    from app.services.evidence import normalize_evidence_ref
    ev = normalize_evidence_ref(item["evidence"], _TEXT)
    assert ev["verification"] == "missing", "定位失败必须降级 missing"
    assert ev["start"] is None and ev["end"] is None
    assert ev["evidence_id"] == "", "降级后不得保留或新发 ID"


def test_normalize_review_evidence_covers_all_containers():
    """第三轮复核 P1-2：读路径归一化必须覆盖全部六个容器——
    每个容器注入一张历史票据（verified 缺坐标），断言全部被归一化。"""
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
    legacy_ev = {
        "document_version": "dv", "quote": "违约金为总额百分之三十",
        "start": None, "end": None, "clause_id": None,
        "verification": "verified", "parse_source": "rules",
        # 门禁 P1（小智娘）：必须注入非空陈旧 ID——空串会让「未归一化则 ID
        # 应被重算」的断言被短路，变异（回滚容器覆盖）下测试不变红
        "evidence_id": "ev-stale12345678",
    }

    def _plant(obj):
        if isinstance(obj, dict):
            obj["evidence"] = dict(legacy_ev)
        elif isinstance(obj, list) and obj:
            obj[0]["evidence"] = dict(legacy_ev)

    _plant((row.get("items") or [{}])[0])
    _plant((row.get("blind_candidates") or [{}])[0])
    quality = row.get("quality") or {}
    _plant((quality.get("observations") or [{}])[0])
    _plant((quality.get("facts") or [{}])[0])
    _plant((row.get("facts") or [{}])[0])
    objections = row.get("objections") or {}
    if isinstance(objections, dict) and objections.get("objections"):
        _plant(objections["objections"][0])
    verify = row.get("verify") or {}
    if isinstance(verify, dict) and verify.get("questions"):
        _plant(verify["questions"][0])
    store_module.update(rid, items=row["items"], blind_candidates=row["blind_candidates"],
                        quality=row["quality"], facts=row["facts"],
                        objections=row["objections"], verify=row["verify"])

    body = client.get(f"/api/review/{rid}").json()

    def _ev_of(obj):
        if isinstance(obj, dict):
            return obj.get("evidence")
        if isinstance(obj, list) and obj:
            return obj[0].get("evidence")
        return None

    checked = 0
    for container in (
        body["items"],
        body.get("blind_candidates") or [],
        (body.get("quality") or {}).get("observations") or [],
        (body.get("quality") or {}).get("facts") or [],
        body.get("facts") or [],
        ((body.get("objections") or {}).get("objections") or []),
        ((body.get("verify") or {}).get("questions") or []),
    ):
        ev = _ev_of(container)
        if ev is None:
            continue
        checked += 1
        # 该历史票的 quote 在 fixture 文本里可能定位不到 → 必须降级 missing 且无 ID；
        # 定位得到 → 必须补坐标并重算 ID。两种结果都不得保留「verified+无坐标+有 ID」
        assert not (ev.get("verification") in ("verified", "ambiguous")
                    and ev.get("start") is None and ev["evidence_id"]), \
            "容器残留 verified+无坐标+有 ID 的票据（P1-2 漏网）"
    assert checked >= 4, f"应至少覆盖 4 个容器，实际 {checked}"


# ---------- PR review 三连销项（P1-a verify 子路由 / P1-b sibling 同步 / P2 版本回填） ----------

def _done_row_with_verify_question(quote: str, *, dv_in_ticket: str, stale_id: str):
    """造一条 done 记录：verify.questions[0] 挂一张「verified 缺坐标」历史票。

    quote 固定用不在 fixture 文本里的句子 → 归一化必须降级 missing。
    dv_in_ticket 控制票据自带版本（P2 用空版本验证回填）。
    """
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
    row["verify"] = {
        "available": True,
        "questions": [{
            "id": "q1",
            "source": "fact",
            "status": "pending",
            "question": "摘句是否属实？",
            "quote": quote,
            # P1-b 的钉子：问题级 sibling verification 也挂着「已验证」
            "verification": "verified",
            "evidence": {
                "document_version": dv_in_ticket,
                "quote": quote,
                "start": None, "end": None, "clause_id": None,
                "verification": "verified", "parse_source": "fact",
                "evidence_id": stale_id,
            },
        }],
    }
    store_module.update(rid, verify=row["verify"])
    return client, rid


def test_verify_endpoint_normalizes_and_syncs_sibling():
    """PR review P1-a+P1-b：verify 子路由必须归一化，且问题级 verification
    随票据同步——票据降 missing 后 UI 的「摘句对得上」不得继续显示 verified。"""
    client, rid = _done_row_with_verify_question(
        "这句话绝不在合同原文之中", dv_in_ticket="dv", stale_id="ev-staleverify1",
    )
    body = client.get(f"/api/review/{rid}/verify").json()
    q = body["questions"][0]
    assert q["evidence"]["verification"] == "missing", "verify 出口必须归一化（P1-a）"
    assert q["evidence"]["evidence_id"] == "", "陈旧 ID 不得从 verify 出口漏出"
    assert q["verification"] == "missing", "问题级 verification 必须随票据同步（P1-b）"
    # confirm 响应同样走归一化（P1-a 第二出口：确认后 UI 会被此响应刷写）
    r = client.post(f"/api/review/{rid}/confirm", json={"question_id": "q1", "choice": "confirm"})
    assert r.status_code == 200
    q2 = r.json()["verify"]["questions"][0]
    assert q2["evidence"]["verification"] == "missing"
    assert q2["verification"] == "missing"


def test_empty_document_version_backfilled_before_id():
    """PR review P2：旧票据空 document_version 必须回填行级版本再算 ID——
    否则不同合同的同摘句会同 ID，击穿 document 维度身份隔离。"""
    from app.services.evidence import normalize_verify_state

    # 回填后：与「用行级版本直接构造的票据」同 ID
    row_ev = {
        "document_version": "", "quote": "违约金为总额百分之三十",
        "start": 4, "end": 16, "clause_id": None,
        "verification": "verified", "parse_source": "fact", "evidence_id": "",
    }
    q = {"evidence": row_ev}
    out = normalize_verify_state(
        {"questions": [q]}, text=_TEXT, document_version="rv1:abc",
    )
    got = out["questions"][0]["evidence"]
    expect = dict(row_ev, document_version="rv1:abc")
    assert got["evidence_id"] == evidence_id_for(expect), \
        "ID 必须按回填后的版本计算（空版本直哈希会跨合同撞车）"
    # 对照：不同行的同摘句必须不同 ID
    other = normalize_verify_state(
        {"questions": [dict(q)]}, text=_TEXT, document_version="rv2:def",
    )
    assert other["questions"][0]["evidence"]["evidence_id"] != got["evidence_id"]
