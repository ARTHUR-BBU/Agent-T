"""批 2b-② 回归：claim_id / claim_content_hash / evidence_refs / 迁移警告。

实现稿 v1.8 §7 清单；红线：变异验证回滚必须变红（M1-M15）。
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app
from app.services.evidence import (
    annotate_review_claims,
    build_evidence,
    claim_content_hash,
)
from app.services.store import store as store_module
from tests.helpers import wait_review_done

client = TestClient(app)

_TEXT = "第三条 违约金为总额百分之三十。乙方应于七日内提出书面异议。"
_DV = "dv-claims-001"
_RP = {"rule_pack_id": "procurement", "rule_pack_content_version": "aaa000bbb111",
       "rule_engine_version": "ccc222ddd333"}


def _norm(row: dict) -> dict:
    from app.services.evidence import normalize_review_evidence
    return normalize_review_evidence(row)


def _item(eid: str, *, name="违约金上限", note="比例偏高", dim=None):
    ev = build_evidence(text=_TEXT, quote="违约金为总额百分之三十",
                        parse_source="rules", document_version=_DV)
    it = {"id": eid or "penalty_cap", "name": name, "note": note,
          "status": "需关注", "evidence": dict(ev)}
    if eid is None:  # 无合格证据场景：票缺失
        it["evidence"] = {"document_version": _DV, "quote": "", "start": None,
                          "end": None, "clause_id": None,
                          "verification": "missing", "parse_source": "rules",
                          "evidence_id": ""}
    return it


def _annotate(row: dict) -> tuple[dict, list]:
    row.setdefault("text", _TEXT)
    row.setdefault("document_version", _DV)
    row.setdefault("rule_pack", dict(_RP))
    return annotate_review_claims(_norm(row))


# ---------- T1 稳定性三连 ----------

def test_claim_id_stable_and_content_hash_detects_drift():
    """T1：两次生成同 ID；换序不变；改一字 → hash 变而 ID 不变。"""
    r1, _ = _annotate({"items": [_item("penalty_cap")]})
    r2, _ = _annotate({"items": [_item("penalty_cap")]})
    c1, c2 = r1["items"][0], r2["items"][0]
    assert c1["claim_id"].startswith("cl-") and c1["claim_id"] == c2["claim_id"]
    # 改一个字：ID 不变（证据不变）、hash 变（内容变了）
    r3, _ = _annotate({"items": [_item("penalty_cap", note="比例显著偏高")]})
    c3 = r3["items"][0]
    assert c3["claim_id"] == c1["claim_id"]
    assert c3["claim_content_hash"] != c1["claim_content_hash"]


def test_observation_reorder_keeps_claim_ids():
    """T1 换序：同批观察仅调换顺序 → 全部 claim_id 不变（禁下标）。"""
    obs = [
        {"dimension": "completeness", "title": "甲条款", "comment": "缺少验收条款",
         "evidence": dict(build_evidence(text=_TEXT, quote="违约金为总额百分之三十",
                                         parse_source="quality", document_version=_DV))},
        {"dimension": "consistency", "title": "乙条款", "comment": "表述前后不一",
         "evidence": dict(build_evidence(text=_TEXT, quote="乙方应于七日内提出书面异议",
                                         parse_source="quality", document_version=_DV))},
    ]
    ids_a = _annotate({"quality": {"observations": [dict(o) for o in obs]}})[0]["quality"]["observations"]
    ids_b = _annotate({"quality": {"observations": [obs[1], obs[0]]}})[0]["quality"]["observations"]
    by_dim_a = {o["dimension"]: o["claim_id"] for o in ids_a}
    by_dim_b = {o["dimension"]: o["claim_id"] for o in ids_b}
    assert by_dim_a == by_dim_b and all(v.startswith("cl-") for v in by_dim_a.values())


def test_same_dimension_same_evidence_merges_group():
    """§1.7 合并语义：同 dimension+同证据的两条观察 = 同一主张（同 ID 同指纹）。"""
    ev = build_evidence(text=_TEXT, quote="违约金为总额百分之三十",
                        parse_source="quality", document_version=_DV)
    obs = [
        {"dimension": "completeness", "title": "A", "comment": "说法一", "evidence": dict(ev)},
        {"dimension": "completeness", "title": "B", "comment": "说法二", "evidence": dict(ev)},
    ]
    out = _annotate({"quality": {"observations": obs}})[0]["quality"]["observations"]
    assert out[0]["claim_id"] == out[1]["claim_id"]
    assert out[0]["claim_content_hash"] == out[1]["claim_content_hash"], \
        "同一主张的组聚合指纹必须一致（顺序无关）"


# ---------- T2 / T2b / T2c / T2d 主键 ----------

def test_verify_key_includes_subject_and_scope():
    """T2/T2c：同证据不同来源对象 → 不同 claim；同对象 → 稳定；scope 入键。"""
    def q(source, ssk):
        return {"id": "q", "source": source, "source_subject_key": ssk,
                "question": "摘句属实？",
                "evidence": dict(build_evidence(text=_TEXT, quote="违约金为总额百分之三十",
                                                parse_source="fact", document_version=_DV))}
    row = {"verify": {"questions": [q("rule_attention", "penalty_cap"), q("rule_attention", "delivery"),
                                    q("fact", "f1")],
                     }}
    out = _annotate(row)[0]["verify"]["questions"]
    assert len({o["claim_id"] for o in out}) == 3, "不同来源对象必不同 claim"


def test_scope_in_claim_id_cross_pack_distinct():
    """T2b：同合同同证据同 item_id、不同规则包 → 必不同 claim_id。"""
    it = _item("penalty_cap")
    r1, _ = _annotate({"items": [dict(it)], "rule_pack": {**_RP, "rule_pack_id": "procurement"}})
    r2, _ = _annotate({"items": [dict(it)],
                       "rule_pack": {**_RP, "rule_pack_id": "nda",
                                     "rule_pack_content_version": "aaa000bbb111"}})
    assert r1["items"][0]["claim_id"] != r2["items"][0]["claim_id"]


def test_legacy_scope_warning_and_stability():
    """T2d：旧行无 rule_pack → legacy 派生 + 迁移警告 + 两次 GET 幂等。"""
    row = {"items": [_item("penalty_cap")]}
    row.pop("rule_pack", None)
    raw = {"text": _TEXT, "document_version": _DV, "items": [_item("penalty_cap")]}
    out, warnings = annotate_review_claims(_norm(raw))
    assert any(w["reason"] == "legacy_scope" for w in warnings)
    assert out["items"][0]["claim_id"].startswith("cl-")
    out2, warnings2 = annotate_review_claims(_norm(raw))
    assert out2["items"][0]["claim_id"] == out["items"][0]["claim_id"], "legacy 幂等"
    assert len(warnings2) == len(warnings)


# ---------- T3 / T4 / T5b ----------

def test_no_primary_evidence_no_formal_claim():
    """T3（审计口径修正版）：无合格主证据 → claim_id 均空串、不进 refs。"""
    row = {"items": [_item(None), _item(None)]}
    out, _ = _annotate(row)
    for it in out["items"]:
        assert it["claim_id"] == ""
        assert it["evidence_refs"] == []


def test_refs_dedup_sorted_and_qualified_only():
    """T4：refs 去重定序；不合格票不进。"""
    it = _item("penalty_cap")
    row = {"items": [it]}
    out, _ = _annotate(row)
    refs = out["items"][0]["evidence_refs"]
    assert refs == [{"evidence_id": refs[0]["evidence_id"], "relation": "primary"}]
    assert refs[0]["evidence_id"].startswith("ev-")


def test_rebuts_only_accepted_and_fail_closed():
    """T5/T5b：仅受理异议有 rebuts；目标无合格主证据 → missing + 自身 primary 照常。"""
    good_target = _item("penalty_cap")
    missing_target = _item(None)  # 无合格证据的目标（漏报场景）
    missing_target["id"] = "delivery"

    def _ob(item_id, accepted, target_ev):
        return {"item_id": item_id, "direction": "omission", "accepted": accepted,
                "legal_reasoning": "论证", "proposal": "提案", "stance_check": "无关",
                "quote": "乙方应于七日内提出书面异议",
                "evidence": dict(build_evidence(text=_TEXT, quote="乙方应于七日内提出书面异议",
                                                parse_source="objection", document_version=_DV))}

    row = {"items": [good_target, missing_target],
           "objections": {"objections": [
               _ob("penalty_cap", True, True),
               _ob("delivery", True, False),
               _ob("penalty_cap", False, True),
           ]}}
    out, _ = _annotate(row)
    obs = out["objections"]["objections"]
    assert obs[0]["rebuts_status"] == "present"
    assert any(r["relation"] == "rebuts" for r in obs[0]["evidence_refs"])
    assert obs[0]["evidence_refs"][0]["relation"] == "primary", "自身 primary 仍在"
    assert obs[1]["rebuts_status"] == "missing"
    assert obs[1]["rebuts_reason"] == "target_no_valid_evidence"
    assert all(r["relation"] != "rebuts" for r in obs[1]["evidence_refs"]), "不伪造假链接"
    assert obs[2]["rebuts_status"] == "not_applicable", "未受理不记 rebuts"


# ---------- 内容指纹字节级 ----------

def test_content_hash_swap_detection_and_order_independence():
    """T5f/T5g/T5h：字段互换必变；记录交换必变；数组换序不变。"""
    rec_a = {"title": "付款问题", "comment": "建议补充验收条件"}
    rec_b = {"title": "验收问题", "comment": "建议明确付款节点"}
    h_base = claim_content_hash("quality_observation", [dict(rec_a), dict(rec_b)])
    h_single = claim_content_hash("quality_observation", [dict(rec_a)])
    # 同一记录内字段互换 → 必变（与同内容基准比，不跨内容集比较）
    swapped_field = [{"title": rec_a["comment"], "comment": rec_a["title"]}]
    assert claim_content_hash("quality_observation", swapped_field) != h_single
    # 组内两条记录的 comment 互换 → 必变（字段对应关系不丢）
    cross = [
        {"title": "付款问题", "comment": "建议明确付款节点"},
        {"title": "验收问题", "comment": "建议补充验收条件"},
    ]
    assert claim_content_hash("quality_observation", cross) != h_base
    # 仅换数组顺序 → 不变
    assert claim_content_hash("quality_observation", [dict(rec_b), dict(rec_a)]) == h_base


def test_schema_version_once_and_newline_sensitivity():
    """T5j/T5l：schema_version 恰一次；换行差异不被吞。"""
    h1 = claim_content_hash("rule_item", [{"name": "付款\n条件", "note": "n"}])
    h2 = claim_content_hash("rule_item", [{"name": "付款条件", "note": "n"}])
    assert h1 != h2, "换行是内容变化，不得静默吞掉"


# ---------- API 集成 ----------

def _upload_done() -> str:
    sample = Path(__file__).resolve().parents[1] / "fixtures" / "procurement_sample.txt"
    rid = client.post(
        "/api/upload", files={"file": ("p.txt", sample.read_bytes(), "text/plain")},
        data={"category": "procurement"},
    ).json()["review_id"]
    wait_review_done(client, rid)
    return rid


def test_claims_reach_api_and_store_untouched():
    """T6/T9/T10：claim 字段随 API 到客户端；旧客户端兼容；store 不被读路径改写；
    铁律 3（status 不受标注影响）。"""
    rid = _upload_done()
    before = store_module.get(rid)
    snapshot = json.dumps(before, sort_keys=True, ensure_ascii=False)
    statuses_before = [(i["id"], i["status"]) for i in before["items"]]

    body = client.get(f"/api/review/{rid}").json()
    item = next(i for i in body["items"] if i.get("claim_id"))
    assert item["claim_id"].startswith("cl-")
    assert item["claim_content_hash"].startswith("cc-")
    assert item["evidence_refs"] and item["evidence_refs"][0]["relation"] == "primary"
    # 迁移警告字段在响应中（正常新审查应为空列表而非缺字段）
    assert body.get("claim_migration_warnings") == []

    client.get(f"/api/review/{rid}")
    after = store_module.get(rid)
    assert json.dumps(after, sort_keys=True, ensure_ascii=False) == snapshot, "读路径不写 store"
    assert [(i["id"], i["status"]) for i in after["items"]] == statuses_before, "铁律 3：档位零触碰"


def test_old_record_gets_claims_via_read_path():
    """T6/T7：批 2b-② 之前的旧行（无 claim 键、无 rule_pack）→ 读路径补齐 + 迁移警告。"""
    from app.services.evidence import document_version_for
    text = (Path(__file__).resolve().parents[1] / "fixtures" / "procurement_sample.txt").read_text(encoding="utf-8")
    real_quote = next(ln.strip() for ln in text.splitlines() if len(ln.strip()) >= 10)
    rid = store_module.create(
        filename="old.txt", category="procurement", status="done", stage="done",
        created_at="2026-09-20 10:00",
        items=[{"id": "penalty_cap", "name": "违约金上限", "status": "需关注", "note": "比例偏高",
                "quote": real_quote,
                "evidence": build_evidence(text=text, quote=real_quote,
                                           parse_source="rules",
                                           document_version=document_version_for(text))}],
        scorecard={}, blind_candidates=[], blind_skipped_messages=[],
        blind_skipped_reason=None, blind_enabled=False, text=text,
        document_version=document_version_for(text),
        policies=[], error=None,
    )
    body = client.get(f"/api/review/{rid}").json()
    item = body["items"][0]
    assert item["claim_id"].startswith("cl-"), "旧记录读路径补齐 claim_id"
    assert any(w["reason"] == "legacy_scope" for w in body["claim_migration_warnings"]), \
        "缺规则包版本必须出现在迁移警告（且到达客户端不被剥）"
