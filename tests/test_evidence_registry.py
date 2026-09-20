"""证据登记簿（批 2a）回归：纯读路径派生视图，每次响应即时重建。

验收四问（审计 2026-09-20）：目录是否准确、是否幂等、是否不泄露原文、
是否完全不写 store。
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app
from app.services.evidence import (
    build_evidence,
    build_evidence_registry,
    normalize_review_evidence,
)
from app.services.store import store as store_module
from tests.helpers import wait_review_done

client = TestClient(app)

_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "procurement_sample.txt"
_QUOTE = "违约金为总额百分之三十"  # _TEXT 里可定位的摘句


def _upload_done() -> str:
    files = {"file": ("p.txt", _FIXTURE.read_bytes(), "text/plain")}
    rid = client.post("/api/upload", files=files, data={"category": "procurement"}).json()["review_id"]
    wait_review_done(client, rid)
    return rid


# ---------- 单元：build_evidence_registry 计数语义 ----------

def _ev(quote: str, *, text: str = "第三条 违约金为总额百分之三十。", **kw):
    return build_evidence(text=text, quote=quote, parse_source="rules", **kw)


def test_multi_source_and_occurrence_unique_split():
    """T1：同票三层 → occurrence 计 3、unique 计 1、multi_source_unique 计 1
    （审计修订二：出现数与唯一数必须分开）。"""
    ev = _ev(_QUOTE)
    row = {
        "document_version": "dv1",
        "text": "第三条 违约金为总额百分之三十。",
        "items": [{"id": "a", "evidence": dict(ev)}],
        "quality": {"observations": [{"id": "b", "evidence": dict(ev)}]},
        "verify": {"questions": [{"id": "c", "evidence": dict(ev)}]},
    }
    reg = build_evidence_registry(normalize_review_evidence(row), [])
    assert reg["occurrence_total"] == 3
    assert reg["unique_total"] == 1
    assert reg["qualified_occurrence_total"] == 3
    assert reg["qualified_unique_total"] == 1
    assert reg["multi_source_unique"] == 1, "同 ID 出现在 items/quality/verify 三个容器必须计多源"


def test_broken_captured_before_normalization_wipes_id():
    """T2（审计修订一核心）：missing 却带旧 ID 的票据，归一化会把 ID 清掉——
    warnings 必须在**改写前**捕获，登记簿才看得见曾经的脏数据。"""
    row = {
        "document_version": "dv1",
        "text": "第三条 违约金为总额百分之三十。",
        "items": [{
            "id": "a",
            "evidence": {
                "document_version": "dv1", "quote": "这句绝不在原文里",
                "start": None, "end": None, "clause_id": None,
                "verification": "missing", "parse_source": "rules",
                "evidence_id": "ev-old",
            },
        }],
    }
    warnings: list = []
    normalized = normalize_review_evidence(row, warnings)
    assert normalized["items"][0]["evidence"]["evidence_id"] == "", "归一化已清 ID"
    assert len(warnings) == 1
    w = warnings[0]
    assert w["where"] == "items[0].evidence"
    assert w["old_evidence_id"] == "ev-old", "改写前的旧 ID 必须被捕获"
    assert w["reason"] == "unqualified_id"
    reg = build_evidence_registry(normalized, warnings)
    assert reg["broken_ref_count"] == 1
    assert reg["broken_refs"][0]["old_evidence_id"] == "ev-old"


def test_warning_downgraded_unlocatable():
    """verified 缺坐标且重新定位失败 → reason=downgraded_unlocatable。"""
    row = {
        "document_version": "dv1",
        "text": "第三条 违约金为总额百分之三十。",
        "items": [{
            "id": "a",
            "evidence": {
                "document_version": "dv1", "quote": "这句绝不在原文里",
                "start": None, "end": None, "clause_id": None,
                "verification": "verified", "parse_source": "rules",
                "evidence_id": "ev-stale",
            },
        }],
    }
    warnings: list = []
    normalize_review_evidence(row, warnings)
    assert warnings and warnings[0]["reason"] == "downgraded_unlocatable"


def test_clean_row_has_zero_warnings():
    """T9：干净记录（归一化零改写）→ broken_ref_count==0。"""
    row = {
        "document_version": "dv1",
        "text": "第三条 违约金为总额百分之三十。",
        "items": [{"id": "a", "evidence": _ev(_QUOTE)}],
    }
    warnings: list = []
    normalize_review_evidence(row, warnings)
    assert warnings == []
    reg = build_evidence_registry(normalize_review_evidence(row), warnings)
    assert reg["broken_ref_count"] == 0 and reg["broken_refs"] == []


def test_id_recomputed_warning_on_version_backfill():
    """空版本回填导致 ID 重算 → reason=id_recomputed（改写前旧 ID 被捕获）。"""
    ev = _ev(_QUOTE)
    ev["document_version"] = ""  # 伪造空版本旧票
    ev["evidence_id"] = "ev-staleversion"
    row = {
        "document_version": "dv-real",
        "text": "第三条 违约金为总额百分之三十。",
        "items": [{"id": "a", "evidence": ev}],
    }
    warnings: list = []
    normalize_review_evidence(row, warnings)
    assert warnings and warnings[0]["reason"] == "id_recomputed"
    assert warnings[0]["old_evidence_id"] == "ev-staleversion"


# ---------- API 集成 ----------

def test_registry_reaches_api_and_is_accurate():
    """T6 + T8：上传 fixture → registry 随 ReviewSummary 到客户端且计数准确。"""
    rid = _upload_done()
    body = client.get(f"/api/review/{rid}").json()
    reg = body.get("evidence_registry")
    assert reg is not None, "evidence_registry 必须出现在响应（schema 缺字段会被静默 ignore）"
    assert reg["registry_version"] == 1
    assert reg["rebuilt_at"]
    # 与容器实际票据数对账（目录准确性）
    def _cnt(objs):
        return sum(1 for o in objs if isinstance(o, dict) and o.get("evidence"))
    q = body.get("quality") or {}
    expected = (
        _cnt(body["items"]) + _cnt(body.get("blind_candidates") or [])
        + _cnt((q.get("observations") or [])) + _cnt(q.get("facts") or [])
        + _cnt(body.get("facts") or [])
        + _cnt(((body.get("objections") or {}).get("objections") or []))
        + _cnt(((body.get("verify") or {}).get("questions") or []))
    )
    assert reg["occurrence_total"] == expected
    assert reg["qualified_unique_total"] >= 1, "正常审查至少有一张合格票"
    # 门禁 P2-2：pipeline 落库前已 canonical 化——正常 fixture 审查的坏账
    # 必须为 0（此前曾因读路径迁移产生警告而弱化为 >=0 恒真，已随落库
    # canonical 化恢复有效断言）
    assert reg["broken_ref_count"] == 0, (
        f"正常 fixture 不应有坏账：{reg['broken_refs']}"
    )


def test_registry_idempotent_across_gets():
    """T3：连续两次 GET → 除 rebuilt_at 外逐字段相等（纯派生无累积状态）。"""
    rid = _upload_done()
    r1 = client.get(f"/api/review/{rid}").json()["evidence_registry"]
    r2 = client.get(f"/api/review/{rid}").json()["evidence_registry"]
    assert r1.pop("rebuilt_at") is not None
    assert r2.pop("rebuilt_at") is not None
    assert r1 == r2


def test_registry_leaks_no_quote_text():
    """T5 隐私契约：registry 序列化后不含任何票据 quote 全文（目录不是第二份合同）。"""
    rid = _upload_done()
    row = store_module.get(rid)
    quotes = [
        i["evidence"].get("quote") or ""
        for i in row["items"] if i.get("evidence")
    ]
    quotes = [q for q in quotes if len(q) >= 8]
    assert quotes, "fixture 应产生带 quote 的票据"
    body = client.get(f"/api/review/{rid}").json()
    reg_text = json.dumps(body["evidence_registry"], ensure_ascii=False)
    for q in quotes:
        assert q not in reg_text, "登记簿不得携带票据 quote 全文"


def test_old_record_without_evidence_keys():
    """T4 旧记录兼容：批 1 之前形态（无 evidence 键）→ 200 且计数全 0。"""
    rid = store_module.create(
        filename="old.txt", category="lease", status="done", stage="done",
        created_at="2026-09-12 10:00", items=[{"id": "x", "name": "甲", "status": "通过"}],
        scorecard={}, blind_candidates=[], blind_skipped_messages=[],
        blind_skipped_reason=None, blind_enabled=False, text="",
        policies=[], error=None,
    )
    body = client.get(f"/api/review/{rid}").json()
    reg = body["evidence_registry"]
    assert reg["occurrence_total"] == 0
    assert reg["unique_total"] == 0
    assert reg["broken_ref_count"] == 0


def test_broken_refs_sample_capped_at_20():
    """T7 响应体积护栏：25 条脏票 → broken_ref_count==25 而样本 ≤20。"""
    row = {
        "document_version": "dv1",
        "text": "第三条 违约金为总额百分之三十。",
        "items": [
            {"id": f"x{i}", "evidence": {
                "document_version": "dv1", "quote": "这句绝不在原文里",
                "start": None, "end": None, "clause_id": None,
                "verification": "missing", "parse_source": "rules",
                "evidence_id": f"ev-old{i:02d}",
            }}
            for i in range(25)
        ],
    }
    warnings: list = []
    normalized = normalize_review_evidence(row, warnings)
    reg = build_evidence_registry(normalized, warnings)
    assert reg["broken_ref_count"] == 25
    assert len(reg["broken_refs"]) == 20


def test_registry_output_keys_frozen_at_source():
    """T5b 单元级隐私契约：build_evidence_registry 输出键集冻结——
    API 层 T5 会被 pydantic 剥未知键「兜底」，源头必须钉死（M4 变异教训：
    向输出塞 quote 的键被 pydantic 静默剥掉，T5 假绿）。"""
    row = {
        "document_version": "dv1",
        "text": "第三条 违约金为总额百分之三十。",
        "items": [{"id": "a", "evidence": _ev(_QUOTE)}],
    }
    reg = build_evidence_registry(normalize_review_evidence(row), [])
    assert set(reg.keys()) == {
        "registry_version", "rebuilt_at", "occurrence_total", "unique_total",
        "qualified_occurrence_total", "qualified_unique_total", "multi_source_unique",
        "broken_ref_count", "broken_refs",
    }, "登记簿输出键集契约冻结：新字段必须走 schema 版本演进，不得夹带明细"
    assert _QUOTE not in json.dumps(reg, ensure_ascii=False)


def test_multi_source_requires_cross_container():
    """门禁 P2-1 钉子：multi_source 按容器名分组——同容器内两个条目持同 ID
    不计多源（保守口径防同层重复膨胀），跨容器才计。"""
    ev = _ev(_QUOTE)
    row = {
        "document_version": "dv1",
        "text": "第三条 违约金为总额百分之三十。",
        "items": [{"id": "a", "evidence": dict(ev)}, {"id": "b", "evidence": dict(ev)}],
    }
    reg = build_evidence_registry(normalize_review_evidence(row), [])
    assert reg["occurrence_total"] == 2
    assert reg["unique_total"] == 1
    assert reg["multi_source_unique"] == 0, "同容器同 ID 不算多源"


def test_rebuilt_at_is_fresh_iso_timestamp():
    """门禁 P3-1 钉子：rebuilt_at 必须是每次响应即时生成的合法 ISO 时间——
    冻结为常量或读旧值都应被抓（T3 原版 pop 掉它测不到）。"""
    from datetime import datetime, timezone

    before = datetime.now(timezone.utc)
    rid = _upload_done()
    r1 = client.get(f"/api/review/{rid}").json()["evidence_registry"]["rebuilt_at"]
    r2 = client.get(f"/api/review/{rid}").json()["evidence_registry"]["rebuilt_at"]
    after = datetime.now(timezone.utc)
    t1 = datetime.fromisoformat(r1)
    t2 = datetime.fromisoformat(r2)
    assert before <= t1 <= after, "rebuilt_at 必须是本次请求窗口内的即时时间"
    assert before <= t2 <= after
    assert r1 != r2 or (t2 - t1).total_seconds() < 1, "两次重建时间戳独立生成"


# ---------- PR review P2-b：登记簿自检（归一化盲区的票据） ----------

def test_registry_catches_skipped_ticket_with_stale_id():
    """P2-b 钉子一：quote 缺失的票据归一化会跳过（_fix 门控），它带着
    missing+旧 ID 混进登记簿时必须被自检抓到并逐出关联账目——否则
    broken_ref_count 恒 0，broken 账目被击穿。"""
    row = {
        "document_version": "dv1",
        "text": "第三条 违约金为总额百分之三十。",
        "items": [{
            "id": "a",
            "evidence": {  # quote 键缺失 → 归一化盲区
                "document_version": "dv1",
                "start": None, "end": None, "clause_id": None,
                "verification": "missing", "parse_source": "rules",
                "evidence_id": "ev-staleskip1",
            },
        }],
    }
    reg = build_evidence_registry(normalize_review_evidence(row), [])
    assert reg["broken_ref_count"] == 1, "登记簿自检必须补上归一化 warnings 的盲区"
    assert reg["broken_refs"][0]["reason"] == "unqualified_id_at_registry"
    assert reg["unique_total"] == 0, "可疑 ID 必须逐出 unique 账目"
    assert reg["multi_source_unique"] == 0


def test_registry_rejects_malformed_id_shape():
    """P2-b 钉子二：合格标签但 ID 形状非法（设计稿 §4.10 前缀防御）——
    记 broken 并逐出 qualified_unique，不参与关联。

    场景必须是归一化盲区（quote 缺失，_fix 跳过）：正常票据的畸形 ID 会被
    归一化顺手重算成合法 ID（走 id_recomputed 警告），到不了登记簿。"""
    row = {
        "document_version": "dv1",
        "text": "第三条 违约金为总额百分之三十。",
        "items": [{
            "id": "a",
            "evidence": {  # quote 键缺失 → 归一化跳过，畸形 ID 原样存活
                "document_version": "dv1",
                "start": 4, "end": 16, "clause_id": None,
                "verification": "verified", "parse_source": "rules",
                "evidence_id": "not-an-ev-id!!",
            },
        }],
    }
    reg = build_evidence_registry(normalize_review_evidence(row), [])
    assert reg["broken_refs"][0]["reason"] == "malformed_id"
    assert reg["qualified_unique_total"] == 0
    assert reg["unique_total"] == 0
