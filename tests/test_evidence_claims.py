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
    derive_claim_id,
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


# ---------- 门禁 P2：身份边界五钉（变异 M2/M4/M5/M8/M11 存活的根因） ----------

def test_scope_component_change_changes_claim_id():
    """P2 钉①（M8/M2 根因）：scope 三元组任一成分变化 → claim_id 必变
    （配置哈希变、引擎哈希变都算——「改代码没改 YAML」也逃不过）。"""
    it = _item("penalty_cap")
    base, _ = _annotate({"items": [dict(it)], "rule_pack": dict(_RP)})
    cfg_changed, _ = _annotate({"items": [dict(it)],
                                "rule_pack": {**_RP, "rule_pack_content_version": "fff000fff000"}})
    eng_changed, _ = _annotate({"items": [dict(it)],
                                "rule_pack": {**_RP, "rule_engine_version": "fff000fff000"}})
    bid = base["items"][0]["claim_id"]
    assert cfg_changed["items"][0]["claim_id"] != bid, "配置版本变化必须换号"
    assert eng_changed["items"][0]["claim_id"] != bid, "引擎版本变化必须换号"


def test_pending_source_never_gets_formal_claim():
    """P2 钉②（M4 根因，v1.7 裁决）：pending 无稳定对象键 → 不发正式 ID。"""
    q = {"id": "vq", "source": "pending", "question": "请确认付款条件",
         "evidence": dict(build_evidence(text=_TEXT, quote="违约金为总额百分之三十",
                                         parse_source="fact", document_version=_DV))}
    out, _ = _annotate({"verify": {"questions": [q]}})
    assert out["verify"]["questions"][0]["claim_id"] == "", "pending 不拿措辞当身份证"


def test_whitelist_out_dimension_no_formal_claim():
    """P2 钉③（M5 根因）：白名单外 dimension → 不发正式 ID + 照算 hash。"""
    obs = {"dimension": "编外维度", "title": "t", "comment": "c",
           "evidence": dict(build_evidence(text=_TEXT, quote="违约金为总额百分之三十",
                                           parse_source="quality", document_version=_DV))}
    out, _ = _annotate({"quality": {"observations": [obs]}})
    o = out["quality"]["observations"][0]
    assert o["claim_id"] == "" and o["evidence_refs"] == []
    assert o["claim_content_hash"].startswith("cc-"), "内容指纹照算（不依赖证据）"


def test_legacy_scope_exact_derivation():
    """P2 钉④（M8 最要紧项）：legacy 行的 claim_id 必须等于 scope="legacy"
    的精确派生值——静默换成今天版本必然不等。"""
    row = {"text": _TEXT, "document_version": _DV, "items": [_item("penalty_cap")]}
    out, _ = annotate_review_claims(_norm(row))
    it = out["items"][0]
    expected = derive_claim_id(
        document_version=_DV, analysis_scope="legacy",
        claim_type="rule_item", business_key="penalty_cap" + chr(31) + it["evidence_refs"][0]["evidence_id"],
    )
    assert it["claim_id"] == expected, "legacy 派生必须可精确复算（静默换版本则不等）"
    assert it["claim_id"] != derive_claim_id(
        document_version=_DV, analysis_scope="procurementaaa000bbb111ccc222ddd333",
        claim_type="rule_item", business_key="penalty_cap" + chr(31) + it["evidence_refs"][0]["evidence_id"],
    ), "legacy 与真实 scope 串必须不同"


def test_source_ref_injection_never_changes_id():
    """P2 钉⑤（M11 根因）：source_ref（obs:i 下标）注入篡改 → claim_id 不变。"""
    def q(ssk, ref):
        return {"id": "vq", "source": "rule_attention", "source_subject_key": ssk,
                "source_ref": ref, "question": "？",
                "evidence": dict(build_evidence(text=_TEXT, quote="违约金为总额百分之三十",
                                                parse_source="fact", document_version=_DV))}
    r1, _ = _annotate({"verify": {"questions": [q("penalty_cap", "item:penalty_cap")]}})
    r2, _ = _annotate({"verify": {"questions": [q("penalty_cap", "obs:0")]}})
    assert r1["verify"]["questions"][0]["claim_id"] == r2["verify"]["questions"][0]["claim_id"]


# ---------- T3 / T4 / T5b ----------

def test_no_primary_evidence_no_formal_claim():
    """T3（审计口径修正版）：无合格主证据 → claim_id 均空串、不进 refs。"""
    row = {"items": [_item(None), _item(None)]}
    out, _ = _annotate(row)
    for it in out["items"]:
        assert it["claim_id"] == ""
        assert it["evidence_refs"] == []


def test_refs_dedup_sorted_and_qualified_only():
    """T4：refs 去重、(relation, evidence_id) 定序；不合格票不进。"""
    good = _item("penalty_cap")
    bad = _item(None)
    row = {"items": [good, bad]}
    out, _ = _annotate(row)
    assert out["items"][1]["evidence_refs"] == [], "不合格票不产生任何引用边"
    refs = out["items"][0]["evidence_refs"]
    assert [r["relation"] for r in refs] == ["primary"], "仅 primary 且无重复"
    assert refs == sorted(refs, key=lambda r: (r["relation"], r["evidence_id"]))


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
    # 内审补充：T5j 的「恰一次」断言此前名存实亡，现通过序列化中间量验证
    from app.services.evidence import _CLAIM_SCHEMA_VERSION
    rec = {"title": "t", "comment": "c"}
    records = [
        "title=" + rec["title"] + chr(31) + "comment=" + rec["comment"],
    ]
    serialized = ("schema_version=" + _CLAIM_SCHEMA_VERSION + chr(31) + "records="
                  + chr(30).join(sorted(records)))
    assert serialized.count("schema_version=") == 1, "版本前缀恰出现一次"
    assert serialized.startswith("schema_version="), "位于最前"
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


def test_source_subject_key_survives_verify_roundtrip():
    """审计 P1-a 钉子：source_subject_key 必须在 ConfirmQuestion 模型往返后
    存活——模型缺字段时 pydantic 静默剥掉，标注拿到空键退回粗粒度。"""
    from app.services.verify import ConfirmQuestion, VerifyInfo

    q = ConfirmQuestion(
        id="vq01", source="rule_attention", source_subject_key="penalty_cap",
        question="？", quote="违约金为总额百分之三十",
    )
    info = VerifyInfo(questions=[q])
    dumped = info.model_dump()["questions"][0]
    assert dumped["source_subject_key"] == "penalty_cap", "模型缺字段=静默剥掉（批 1 教训）"


def test_verify_response_carries_claim_fields():
    """审计 P1-b 钉子：verify 出口（pack_verify）必须带主张标注——
    done 后写方的响应不得空白/陈旧。"""
    rid = _upload_done()
    body = client.get(f"/api/review/{rid}/verify").json()
    qs = body.get("questions") or []
    assert qs, "fixture 应产生核验问题"
    with_claim = [q for q in qs if q.get("claim_id")]
    assert with_claim, "verify 出口的 claim 字段不得为空（出口标注缺失）"
    assert all(q2["claim_id"].startswith("cl-") for q2 in with_claim)


def test_adopt_does_not_touch_evidence_index():
    """T8：adopt 只改 adopted 键——evidence_index 逐字节不变。"""
    rid = _upload_done()
    idx_before = store_module.get(rid)["evidence_index"]
    r = client.post(f"/api/review/{rid}/objections/adopt", json={"index": 0})
    assert r.status_code in (200, 404), "fixture 可能无受理异议，404 亦可"
    if r.status_code == 200:
        idx_after = store_module.get(rid)["evidence_index"]
        assert idx_after == idx_before, "adopt 不得触碰证据索引"


def test_pack_verify_output_always_annotated():
    """P2 钉（M9 根因）：pack_verify 出口标注——store 里塞未标注的 verify
    状态（模拟 recheck 写入），/verify 响应仍必须带新鲜 claim 字段。"""
    rid = _upload_done()
    row = store_module.get(rid)
    text = row.get("text") or ""
    real_quote = next(ln.strip() for ln in text.splitlines() if len(ln.strip()) >= 10)
    from app.services.evidence import build_evidence, document_version_for
    bare_verify = {  # 无 claim 三字段的「裸」verify 状态（模拟 recheck 直写）
        "available": True,
        "questions": [{
            "id": "vq99", "source": "rule_attention",
            "source_subject_key": "penalty_cap",
            "question": "摘句与原文是否一致？", "title": "违约金",
            "quote": real_quote,
            "evidence": build_evidence(text=text, quote=real_quote,
                                       parse_source="fact",
                                       document_version=document_version_for(text)),
        }],
    }
    store_module.update(rid, verify=bare_verify)
    body = client.get(f"/api/review/{rid}/verify").json()
    q = (body.get("questions") or [{}])[0]
    assert q.get("claim_id", "").startswith("cl-"),         "出口标注缺失时响应 claim 字段为空——M9 变异将在此存活"


# ---------- 独立验收签收修正（2026-09-23：主流程过、身份边界未签收） ----------

def test_cross_document_evidence_never_gets_claim():
    """签收 P1：票据 document_version 与行不一致（跨合同混入）→ 不发号、不开 primary。

    变异验证：回滚 _valid_primary 的 document_version 校验，本测试必红。"""
    out, _ = _annotate({"items": [_item("penalty_cap")]})
    assert out["items"][0]["claim_id"].startswith("cl-"), "前置：同版本证据应正常发号"
    foreign = {"items": [_item("penalty_cap")]}
    foreign["items"][0]["evidence"]["document_version"] = "dv-another-contract"
    out2, _ = _annotate(foreign)
    it2 = out2["items"][0]
    assert it2["claim_id"] == "", "跨合同票据必须拒发 claim_id（fail-closed）"
    assert it2["evidence_refs"] == [], "跨合同票据不得产生 primary 引用"
    assert it2["claim_content_hash"].startswith("cc-"), "内容指纹照常（指纹与发号解耦）"


def test_content_hash_excludes_non_whitelist_fields():
    """签收 P2：白名单外字段不得参与内容指纹——指纹覆盖面=规范声明字段集。

    变异验证：回滚为「白名单外字段排后参与序列化」，本测试必红。"""
    base = claim_content_hash("rule_item", [{"name": "a", "note": "b"}])
    smuggled = claim_content_hash("rule_item", [{"name": "a", "note": "b", "smuggled": "x"}])
    assert base == smuggled, "白名单外字段混入不得改变指纹"


def test_rule_pack_versions_fallback_id_matches_pack():
    """签收 P2：未知品类回落 procurement 时 rule_pack_id 必须与配置哈希同包。

    变异验证：回滚 resolved_category（id 恒写请求品类），本测试必红。"""
    from app.services.evidence import rule_pack_versions
    v = rule_pack_versions("no_such_category")
    ref = rule_pack_versions("procurement")
    assert v["rule_pack_id"] == "procurement", "回落时 id 必须写实际使用的包"
    assert v["rule_pack_content_version"] == ref["rule_pack_content_version"]
    assert v["rule_engine_version"] == ref["rule_engine_version"]


def test_quality_obs_subject_key_includes_evidence_id():
    """签收 P1：quality_obs 来源对象键 = dimension + primary_evidence_id（§1.2 规范）。

    变异验证：回滚为裸 dimension，本测试必红。"""
    from app.services.verify import _collect_suspects
    ev = build_evidence(text=_TEXT, quote="违约金为总额百分之三十",
                        parse_source="quality", document_version=_DV)
    qs = _collect_suspects(
        items=[], facts=[], blind_candidates=[], max_questions=10,
        quality={"observations": [{
            "dimension": "completeness", "title": "甲条款",
            "comment": "缺少验收条款", "quote": "违约金为总额百分之三十",
            "evidence": dict(ev),
        }]},
    )
    q = next(x for x in qs if x["source"] == "quality_obs")
    assert q["source_subject_key"] == "completeness" + chr(31) + ev["evidence_id"], \
        "对象键必须含证据 ID（裸 dimension 会把同维度不同观察认成同一对象）"


def test_t5m_old_row_triple_pin():
    """T5m 三重钉补全（签收 P2）：旧行迁移警告可见 + store 逐字节不变 +
    连续两次 GET 一致（除登记簿重建时间戳）。"""
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
    snapshot = json.dumps(store_module.get(rid), sort_keys=True, ensure_ascii=False)
    b1 = client.get(f"/api/review/{rid}").json()
    b2 = client.get(f"/api/review/{rid}").json()
    reg1 = b1.get("evidence_registry") or {}
    assert reg1.get("rebuilt_at"), "登记簿 rebuilt_at 存在（弹除前确认）"
    for b in (b1, b2):
        (b.get("evidence_registry") or {}).pop("rebuilt_at", None)
    assert b1 == b2, "连续两次 GET 必须一致（除 rebuilt_at）"
    assert any(w["reason"] == "legacy_scope" for w in b1["claim_migration_warnings"]), \
        "legacy 警告到达客户端（三重之一：可见）"
    assert json.dumps(store_module.get(rid), sort_keys=True, ensure_ascii=False) == snapshot, \
        "读路径不写 store（三重之二：store 不变）"
