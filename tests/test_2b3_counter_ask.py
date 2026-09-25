"""批 2b-③ 回归：counter_evidence 票据化 + Ask 引用入库 + 三路并发。

实现稿 v1.1（docs/evidence-batch2b3-impl.md）§6 测试矩阵 + §10 四条施工钉。
红线：变异验证回滚必须变红——删锁变异（§5.2）由 test_two_concurrent_asks_
both_recorded 承担，门禁时人工回滚验证该测试必须变红。
"""
from __future__ import annotations

import json
import threading
from datetime import datetime

import pytest

from app.services import llm_ask as llm_ask_service
from app.services import objection as objection_service
from app.services.evidence import (
    annotate_review_claims,
    build_evidence,
    document_version_for,
    rebuild_evidence_index,
)
from app.services.clause_index import build_clause_index
from app.services.store import store as store_module
from tests.test_evidence_claims import client  # noqa: F401 复用模块级 TestClient

_TEXT = (
    "第一条 乙方不得转租，违反的出租方可解除合同；经出租方书面同意的转租有效。\n"
    "第二条 双方对合作内容负有保密义务。\n"
)
_DV = document_version_for(_TEXT)
_RP = {"rule_pack_id": "procurement", "rule_pack_content_version": "aaa000bbb111",
       "rule_engine_version": "ccc222ddd333"}

_ITEMS = [
    {
        "id": "sublet", "name": "转租限制", "status": "需关注",
        "note": "转租禁止叠加解除", "quote": "第一条 乙方不得转租，违反的出租方可解除合同",
        "hits": ["转租"], "rule_id": "sublet#r0", "rule_class": "heuristic",
        "clause_ids": ["c01"], "primary_clause_id": "c01",
    },
]


def _enable(monkeypatch):
    monkeypatch.setenv("OBJECTIONS_ENABLED", "true")


def _payload(**overrides):
    base = {
        "item_id": "sublet",
        "rule_id": "sublet#r0",
        "direction": "false_positive",
        "quote": "第一条 乙方不得转租，违反的出租方可解除合同",
        "counter_evidence": "未发现反证原文",
        "legal_reasoning": "民法典第七百一十六条赋予承租人经同意转租的法定默认安排，"
                           "仅约定「不得转租」并附解除权属于正常权利配置，需结合是否"
                           "实际同意判断，不必然构成真实风险。",
        "stance_check": "与立场无关",
        "proposal": "为「转租」词表增加「经出租方书面同意」反证 unless",
    }
    base.update(overrides)
    return base


_CLAUSE_INDEX = build_clause_index(_TEXT)


def _run(chat_fn):
    return objection_service.run_objections(
        text=_TEXT, items=_ITEMS, chat_fn=chat_fn, document_version=_DV,
        clause_index=_CLAUSE_INDEX,
    )


def _row_with_objections(**overrides):
    """构造含一条受理异议的完整 store 行形状（供 normalize/annotate 用）。"""
    out = _run(lambda s, u: json.dumps({"objections": [_payload()]}, ensure_ascii=False))
    ob = out["objections"][0]
    row = {
        "text": _TEXT,
        "document_version": _DV,
        "rule_pack": dict(_RP),
        "items": [
            {
                "id": "sublet", "name": "转租限制", "status": "需关注",
                "note": "转租禁止叠加解除",
                "quote": "第一条 乙方不得转租，违反的出租方可解除合同",
                "evidence": build_evidence(
                    text=_TEXT, quote="第一条 乙方不得转租，违反的出租方可解除合同",
                    parse_source="rules", document_version=_DV),
            },
        ],
        "objections": {"available": True, "objections": [ob]},
    }
    row.update(overrides)
    return row


def _annotate(row: dict):
    from app.services.evidence import normalize_review_evidence
    return annotate_review_claims(normalize_review_evidence(row))


# ---------- A 组：counter_evidence 票据化（§2） ----------

def test_t_a1_present_counter_gets_ticket_and_counter_edge(monkeypatch):
    _enable(monkeypatch)
    """T-A1：present 反证 → 票据有真实档案位置；claim 标注派生 counter 边；
    counter 与 rebuts 独立记账；业务主键不含反证（换反证不换 claim_id）。"""
    out = _run(lambda s, u: json.dumps(
        {"objections": [_payload(quote="第一条 乙方不得转租，违反的出租方可解除合同", counter_evidence="经出租方书面同意的转租有效")]},
        ensure_ascii=False))
    ob = out["objections"][0]
    assert ob["accepted"] is True
    assert ob["counter_evidence_status"] == "present"
    ref = ob["counter_evidence_ref"]
    assert isinstance(ref, dict) and ref["evidence_id"].startswith("ev-")
    assert ref["verification"] in ("verified", "ambiguous")
    assert ref["parse_source"] == "objection"

    row, _warnings = _annotate(_row_with_objections(
        objections={"available": True, "objections": [out["objections"][0]]}))
    annotated = row["objections"]["objections"][0]
    counter_edges = [r for r in annotated["evidence_refs"] if r["relation"] == "counter"]
    assert counter_edges and counter_edges[0]["evidence_id"] == ref["evidence_id"]
    # 一 primary 一 counter 的不变式；rebuts 独立存在（目标也有合格票）
    assert sum(r["relation"] == "primary" for r in annotated["evidence_refs"]) == 1
    assert any(r["relation"] == "rebuts" for r in annotated["evidence_refs"])

    # 换反证（另一处原文）不换 claim_id：主键 = item_id+direction+primary 票
    out2 = _run(lambda s, u: json.dumps(
        {"objections": [_payload(quote="第一条 乙方不得转租，违反的出租方可解除合同", counter_evidence="经出租方书面同意的转租有效。")]},
        ensure_ascii=False))
    row2, _ = _annotate({
        "text": _TEXT, "document_version": _DV, "rule_pack": dict(_RP),
        "items": row["items"],
        "objections": {"available": True, "objections": [out2["objections"][0]]},
    })
    assert row2["objections"]["objections"][0]["claim_id"] == annotated["claim_id"]


def test_t_a2_absent_declaration_whitespace_normalized(monkeypatch):
    _enable(monkeypatch)
    """T-A2（§10 钉 1）：声明判定空白折叠——前后空格/全角空格仍判 absent，
    不得因格式差异把 absent 误判 missing 或拒收。"""
    variants = [
        "未发现反证原文",
        "  未发现反证原文  ",
        "　未发现反证原文　",  # 全角空格
        "未发现　反证原文",   # 中间全角空格
    ]
    for v in variants:
        out = _run(lambda s, u, _v=v: json.dumps(
            {"objections": [_payload(quote="第一条 乙方不得转租，违反的出租方可解除合同", counter_evidence=_v)]}, ensure_ascii=False))
        ob = out["objections"][0]
        assert ob["accepted"] is True, v
        assert ob["counter_evidence_status"] == "absent", v
        assert ob["counter_evidence_ref"] is None, v


def test_t_a3_missing_reachable_for_accepted_objection(monkeypatch):
    _enable(monkeypatch)
    """T-A3：missing 必须可达——受理异议的反证定位失败（fail-closed 不发票）；
    未受理异议保持空串（不得标 missing）。"""
    # 反证真实存在但不在原文（要件②应拒收 → 未受理 → 空串）
    out = _run(lambda s, u: json.dumps(
        {"objections": [_payload(quote="第一条 乙方不得转租，违反的出租方可解除合同", counter_evidence="这句反证在合同里根本不存在呀")]},
        ensure_ascii=False))
    ob = out["objections"][0]
    assert ob["accepted"] is False
    assert ob["counter_evidence_status"] == ""

    # 制造「通过 quote_supported 但定位失败」的受理路径 → missing 可达
    import app.services.blind_spot as blind_spot
    real_supported = blind_spot.quote_supported

    def fake_supported(text, quote):
        if "不存在的反证" in quote:
            return True
        return real_supported(text, quote)

    real_span_hits = objection_service._quote_span_hits

    def fake_span_hits(text, quote, spans):
        if "不存在的反证" in quote:
            return ["c01"]
        return real_span_hits(text, quote, spans)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(blind_spot, "quote_supported", fake_supported)
        mp.setattr(objection_service, "_quote_span_hits", fake_span_hits)
        out2 = _run(lambda s, u: json.dumps(
            {"objections": [_payload(quote="第一条 乙方不得转租，违反的出租方可解除合同", counter_evidence="不存在的反证原文样例文本")]},
            ensure_ascii=False))
    ob2 = out2["objections"][0]
    assert ob2["accepted"] is True
    assert ob2["counter_evidence_status"] == "missing"
    assert ob2["counter_evidence_ref"] is None
    # missing 不越界：不改写受理与身份账目
    assert ob2["reject_reason"] is None


def test_t_a4_api_roundtrip_keeps_counter_fields(monkeypatch):
    _enable(monkeypatch)
    """T-D1（反证半边）：API Objection 模型不剥 counter_evidence_ref /
    counter_evidence_status（批 1 静默剥字教训——显式断言往返存活）。"""
    from app.api.schemas import Objection as ObjectionInfoModel
    out = _run(lambda s, u: json.dumps(
        {"objections": [_payload(quote="第一条 乙方不得转租，违反的出租方可解除合同", counter_evidence="经出租方书面同意的转租有效")]},
        ensure_ascii=False))
    info = ObjectionInfoModel(**out["objections"][0])
    assert info.counter_evidence_status == "present"
    assert info.counter_evidence_ref is not None
    assert info.counter_evidence_ref.evidence_id.startswith("ev-")


# ---------- B 组：Ask 引用入库（§3 + §10 钉 2/3/4） ----------

def _mk_done_row(**overrides) -> str:
    row = {
        "filename": "ask.txt", "category": "procurement", "status": "done",
        "stage": "done", "created_at": "2026-09-25 10:00",
        "items": [dict(_ITEMS[0])], "scorecard": {}, "blind_candidates": [],
        "blind_skipped_messages": [], "blind_skipped_reason": None,
        "blind_enabled": False, "text": _TEXT,
        "document_version": _DV, "policies": [], "error": None,
    }
    row.update(overrides)
    return store_module.create(**row)


def _ask_ok_result(*, quote: str = "经出租方书面同意的转租有效", ok: bool = True,
                   quote_verified: bool = True, **extra):
    return {
        "ok": ok,
        "answer": {"风险等级": "中", "问题是啥": "解释转租限制"},
        "raw_text": "原始回答文本",
        "quote_verified": quote_verified,
        "evidence": build_evidence(
            text=_TEXT, quote=quote if quote_verified else "",
            parse_source="ask", document_version=_DV,
            force_verification=None if quote_verified else "unverified",
        ),
        **extra,
    }


def test_t_b1_ask_records_entry_without_answer_or_quote(monkeypatch):
    """T-B1：Ask 成功 → row 追加账本元素；五键顶层 + evidence 七键；
    账本与 answer/raw_text 零交集；evidence 不含 quote（§3.2 隐私红线）。"""
    monkeypatch.setattr(llm_ask_service, "ask_about_item",
                        lambda **kw: _ask_ok_result())
    rid = _mk_done_row()
    r = client.post("/api/ask", json={"review_id": rid, "item_id": "sublet",
                                      "question": "这条为什么标需关注？"})
    assert r.status_code == 200 and r.json()["ok"] is True
    stored = store_module.get(rid)
    entries = stored["ask_evidence"]
    assert len(entries) == 1
    entry = entries[0]
    assert set(entry) == {"asked_at", "item_id", "question", "evidence", "quote_verified"}
    assert entry["question"] == "这条为什么标需关注？"
    assert entry["item_id"] == "sublet"
    # 钉 2：asked_at 服务端时钟（ISO 可解析、非入参可伪造）
    datetime.fromisoformat(entry["asked_at"])
    ev = entry["evidence"]
    assert set(ev) == {"evidence_id", "document_version", "clause_id",
                       "start", "end", "verification", "parse_source"}
    assert ev["evidence_id"].startswith("ev-") and ev["verification"] in ("verified", "ambiguous")
    # 隐私红线：回答原文不入账
    assert "answer" not in entry and "raw_text" not in entry
    assert "quote" not in ev
    assert "原始回答文本" not in json.dumps(entries, ensure_ascii=False)


def test_t_b2_gate_rejects_unverified_and_cross_version_tickets(monkeypatch):
    """T-B2/T-B3（§3.3 + 钉 3）：不合格票据不入账，但 Ask 照常回答；
    A 合同票据不能放进 B 合同行（document_version 严格相等门禁）。"""
    rid = _mk_done_row()
    # unverified 票：不入账
    monkeypatch.setattr(llm_ask_service, "ask_about_item",
                        lambda **kw: _ask_ok_result(quote_verified=False))
    r = client.post("/api/ask", json={"review_id": rid, "item_id": "sublet",
                                      "question": "问题一"})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert store_module.get(rid).get("ask_evidence") is None

    # 跨版本票：行的 document_version 与票据不一致 → 不入账
    rid_b = _mk_done_row(document_version="other-contract-dv")
    monkeypatch.setattr(llm_ask_service, "ask_about_item",
                        lambda **kw: _ask_ok_result())
    r = client.post("/api/ask", json={"review_id": rid_b, "item_id": "sublet",
                                      "question": "问题二"})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert store_module.get(rid_b).get("ask_evidence") is None


def test_t_b4_asked_at_always_server_clock(monkeypatch):
    """钉 2：结果里伪造 asked_at，入账时必须被服务器时间覆盖。"""
    entry = llm_ask_service.build_ask_evidence_entry(
        question="q", item_id="sublet",
        result=_ask_ok_result(asked_at="1999-01-01T00:00:00+00:00"),
        row_document_version=_DV, row_text=_TEXT,
    )
    assert entry is not None
    assert entry["asked_at"] != "1999-01-01T00:00:00+00:00"
    datetime.fromisoformat(entry["asked_at"])  # 合法 ISO


def test_t_b5_ask_ticket_reaches_registry_and_index(monkeypatch):
    """T-B5：Ask 票进登记簿与索引（v1.0 P2 缺口——票进账本必须查得到）。"""
    monkeypatch.setattr(llm_ask_service, "ask_about_item",
                        lambda **kw: _ask_ok_result())
    rid = _mk_done_row()
    before = client.get(f"/api/review/{rid}").json()["evidence_registry"]
    r = client.post("/api/ask", json={"review_id": rid, "item_id": "sublet",
                                      "question": "再问一条"})
    assert r.status_code == 200
    after = client.get(f"/api/review/{rid}").json()["evidence_registry"]
    assert after["occurrence_total"] == before["occurrence_total"] + 1
    assert after["qualified_occurrence_total"] == before["qualified_occurrence_total"] + 1
    # 索引：ask 票按 span 注册
    stored = store_module.get(rid)
    ev = stored["ask_evidence"][0]["evidence"]
    key = f"{_DV}|{ev.get('clause_id') or ''}|{ev['start']}|{ev['end']}"
    index = rebuild_evidence_index(store_module.get(rid))
    assert index["by_span"].get(key) == ev["evidence_id"]


def test_t_b6_ttl_zero_never_records(monkeypatch):
    """T-B6（§3.5 规则四）：TTL=0（永不过期）时不登记——宁缺毋滥。"""
    monkeypatch.setattr(store_module, "_ttl", 0.0)
    assert store_module.ttl_seconds == 0.0
    monkeypatch.setattr(llm_ask_service, "ask_about_item",
                        lambda **kw: _ask_ok_result())
    rid = _mk_done_row()
    r = client.post("/api/ask", json={"review_id": rid, "item_id": "sublet",
                                      "question": "过期场景"})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert store_module.get(rid).get("ask_evidence") is None


def test_t_b7_cap_enforced_and_env_strict(monkeypatch):
    """T-B7（§3.5/Q3）：row 上限生效（超限不入账不计错误）；env 非法值
    fail-fast（负数/非数字/空串启动即报错）。"""
    monkeypatch.setattr(llm_ask_service, "MAX_ASK_EVIDENCE_ENTRIES", 1)
    monkeypatch.setattr(llm_ask_service, "ask_about_item",
                        lambda **kw: _ask_ok_result())
    rid = _mk_done_row()
    from app.api.routes import _record_ask_evidence
    from app.api.schemas import AskRequest
    req = AskRequest(review_id=rid, item_id="sublet", question="第一问")
    result = _ask_ok_result()
    row = store_module.get(rid)
    _record_ask_evidence(review_id=rid, row=row, item=row["items"][0],
                         body=req, result=result)
    assert len(store_module.get(rid)["ask_evidence"]) == 1
    _record_ask_evidence(review_id=rid, row=row, item=row["items"][0],
                         body=req, result=result)
    assert len(store_module.get(rid)["ask_evidence"]) == 1, "超限后不再入账"

    for bad in ("-1", "abc", ""):
        monkeypatch.setenv("MAX_ASK_EVIDENCE_ENTRIES", bad)
        with pytest.raises(RuntimeError):
            llm_ask_service._parse_max_ask_evidence_entries()
    monkeypatch.setenv("MAX_ASK_EVIDENCE_ENTRIES", "7")
    assert llm_ask_service._parse_max_ask_evidence_entries() == 7


def test_t_b8_count_only_valid_tickets(monkeypatch):
    """T-B8（钉 4）：ask_evidence_count 只计合格票——坏票不入计数。"""
    rid = _mk_done_row(ask_evidence=[
        {  # 合格
            "asked_at": "2026-09-25T00:00:00+00:00", "item_id": "sublet",
            "question": "好票", "quote_verified": True,
            "evidence": {"evidence_id": "ev-" + "a" * 12, "document_version": _DV,
                         "clause_id": "c02", "start": 10, "end": 20,
                         "verification": "verified", "parse_source": "ask"},
        },
        {  # 坏票：缺 ID
            "asked_at": "2026-09-25T00:00:00+00:00", "item_id": "sublet",
            "question": "坏票一", "quote_verified": False,
            "evidence": {"evidence_id": "", "document_version": _DV,
                         "clause_id": None, "start": None, "end": None,
                         "verification": "unverified", "parse_source": "ask"},
        },
        {  # 坏票：资格态不符
            "asked_at": "2026-09-25T00:00:00+00:00", "item_id": "sublet",
            "question": "坏票二", "quote_verified": False,
            "evidence": {"evidence_id": "ev-" + "b" * 12, "document_version": _DV,
                         "clause_id": "c02", "start": 1, "end": 2,
                         "verification": "missing", "parse_source": "ask"},
        },
        {  # 坏票：跨版本
            "asked_at": "2026-09-25T00:00:00+00:00", "item_id": "sublet",
            "question": "坏票三", "quote_verified": True,
            "evidence": {"evidence_id": "ev-" + "c" * 12, "document_version": "other",
                         "clause_id": "c02", "start": 1, "end": 2,
                         "verification": "verified", "parse_source": "ask"},
        },
    ])
    body = client.get(f"/api/review/{rid}").json()
    assert body["ask_evidence_count"] == 1


# ---------- C 组：三路并发（§5——必须能制造真实写入冲突） ----------

def _run_concurrent_asks(monkeypatch, n: int = 2) -> list[dict]:
    """n 路并发 ask（真实端点写路径）；store.get 注入屏障强制两线程在
    「锁内重读」处会师——制造陈旧读冲突（§5.1：不能靠运气撞竞态）。

    持锁时：先到者等屏障，后到者被锁挡住无法会师 → 屏障超时破裂（1.5s），
    先到者按 fresh 行继续 → 两票都活。
    删锁变异后：两线程同时会师、同读陈旧空账本 → 各写各的 → 丢一票 → 测试红。
    """
    monkeypatch.setattr(llm_ask_service, "ask_about_item",
                        lambda **kw: _ask_ok_result())
    rid = _mk_done_row()
    barrier = threading.Barrier(n)
    local = threading.local()
    orig_get = store_module.get

    def staged_get(rid_: str):
        res = orig_get(rid_)
        if rid_ == rid:
            n_calls = getattr(local, "n", 0) + 1
            local.n = n_calls
            if n_calls == 2:  # 第 2 次 = 写路径锁内重读（§3.4 四步之「读」）
                try:
                    barrier.wait(timeout=1.5)
                except threading.BrokenBarrierError:
                    pass  # 持锁路径：另一线程到不了这里，超时破裂是预期
        return res

    monkeypatch.setattr(store_module, "get", staged_get)

    errors: list[Exception] = []

    def worker(i: int) -> None:
        try:
            r = client.post("/api/ask", json={"review_id": rid, "item_id": "sublet",
                                              "question": f"并发第 {i} 问"})
            assert r.status_code == 200, r.text
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not errors, errors
    return store_module.get(rid)["ask_evidence"]


def test_two_concurrent_asks_both_recorded(monkeypatch):
    """T-C1：两路并发 Ask 不互盖（删锁变异：本测试必须变红——§5.2）。"""
    entries = _run_concurrent_asks(monkeypatch, n=2)
    assert entries is not None and len(entries) == 2, (
        f"并发写丢票：{entries}")
    assert {e["question"] for e in entries} == {"并发第 0 问", "并发第 1 问"}


def test_three_way_concurrent_ask_ask_adopt_no_clobber(monkeypatch):
    _enable(monkeypatch)
    """T-C2：Ask+Ask+Objection 三路并发——同键（ask_evidence）互不覆盖，
    跨键（objections.adopted）各自存活。"""
    monkeypatch.setattr(llm_ask_service, "ask_about_item",
                        lambda **kw: _ask_ok_result())
    # 预置一条受理异议供 adopt
    out = _run(lambda s, u: json.dumps(
        {"objections": [_payload(quote="第一条 乙方不得转租，违反的出租方可解除合同", counter_evidence="经出租方书面同意的转租有效")]},
        ensure_ascii=False))
    rid = _mk_done_row(objections={"available": True, "objections": out["objections"]})
    r = client.post(f"/api/review/{rid}/objections/adopt", json={"index": 0})
    assert r.status_code == 200
    entries = _run_concurrent_asks(monkeypatch, n=2)
    assert len(entries) == 2, "ask 并发丢票"
    stored = store_module.get(rid)
    assert stored["objections"]["objections"][0]["adopted"] is True, "adopt 被并发冲掉"


# ---------- D 组：兼容与派生（§6 通用验收） ----------

def test_old_row_byte_identical_plus_new_keys(monkeypatch):
    _enable(monkeypatch)
    """旧行（无 counter 键 / 无 ask_evidence）读路径行为兼容：登记簿计数
    不含新容器、claim 标注不变、GET 不写 store。"""
    row = _row_with_objections()
    # 抹掉 2b-③ 新键 = 旧行形状
    row["objections"]["objections"][0].pop("counter_evidence_status")
    row["objections"]["objections"][0].pop("counter_evidence_ref")
    rid = store_module.create(**{
        "filename": "old23.txt", "category": "procurement", "status": "done",
        "stage": "done", "created_at": "2026-09-20 10:00",
        "scorecard": {}, "blind_candidates": [], "blind_skipped_messages": [],
        "blind_skipped_reason": None, "blind_enabled": False, "error": None,
        **row,
    })
    import json as _json
    before = store_module.get(rid)
    snap = _json.dumps(before, sort_keys=True, ensure_ascii=False)

    from app.services.evidence import build_evidence_registry, normalize_review_evidence
    normalized = normalize_review_evidence(before)
    reg = build_evidence_registry(normalized, [])
    assert reg["occurrence_total"] == 2  # items 主票 + 异议主票（无反证票无 ask 票）

    client.get(f"/api/review/{rid}")
    after = store_module.get(rid)
    assert _json.dumps(after, sort_keys=True, ensure_ascii=False) == snap, "GET 不写 store"
