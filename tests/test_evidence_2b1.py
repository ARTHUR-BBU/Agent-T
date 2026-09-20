"""批 2b-① 机制回归：span 复用收敛 + evidence_index 缓存生命周期。

终审范围：证据规范化、span 复用、evidence_index（含并发零丢失）。
验收口径：目录准确 / 幂等 / 不泄露原文 / 不写 store（2a 四问延续）+
同 span 必同 ID（canonical 收敛）。
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app
from app.services.evidence import (
    build_evidence,
    normalize_review_evidence,
    rebuild_evidence_index,
    resolve_or_build_evidence,
)
from app.services.store import store as store_module
from tests.helpers import wait_review_done

client = TestClient(app)

_TEXT = "第三条 违约金为总额百分之三十。乙方应于七日内提出书面异议。"
_DV = "dv-test-001"


# ---------- canonical 收敛：同 span 必同 ID ----------

def test_same_span_different_quotes_converge_to_one_id():
    """复用三场景之核心：同一位置的两种措辞变体（带句号/不带）→
    归一化后同一 span 同一 ID——「各自抄原文」变成「引用同一张身份证」。"""
    q1 = "违约金为总额百分之三十"
    q2 = " 违约金为总额百分之三十。"  # 带首尾空白与句号的变体
    t1 = build_evidence(text=_TEXT, quote=q1, parse_source="rules", document_version=_DV)
    t2 = build_evidence(text=_TEXT, quote=q2, parse_source="quality", document_version=_DV)
    n1 = normalize_evidence(t1)
    n2 = normalize_evidence(t2)
    assert n1["evidence_id"] and n2["evidence_id"]
    assert n1["evidence_id"] == n2["evidence_id"], "同 span 措辞变体必须收敛到同一 ID"
    assert n1["quote"] == n2["quote"], "canonical quote = 原文切片"


def normalize_evidence(t: dict) -> dict:
    return normalize_review_evidence(
        {"text": _TEXT, "document_version": _DV, "items": [{"id": "x", "evidence": dict(t)}]},
    )["items"][0]["evidence"]


def test_different_spans_never_converge():
    """不同真实位置 → 不同 ID（不硬合并）。"""
    q1 = "违约金为总额百分之三十"
    q2 = "乙方应于七日内提出书面异议"
    t1 = normalize_evidence(build_evidence(text=_TEXT, quote=q1, parse_source="rules", document_version=_DV))
    t2 = normalize_evidence(build_evidence(text=_TEXT, quote=q2, parse_source="rules", document_version=_DV))
    assert t1["evidence_id"] != t2["evidence_id"]


# ---------- resolve_or_build + 索引 ----------

def test_resolve_or_build_registers_and_flags_duplicate():
    """发证窗口：登记 by_span；同 span 异 ID（旧数据混合）→ duplicate_span
    警告 + 固定收敛（施工纪律 1）。"""
    index: dict = {"version": 1, "by_span": {}}
    t1, w1 = resolve_or_build_evidence(
        text=_TEXT, quote="违约金为总额百分之三十", parse_source="rules",
        document_version=_DV, index=index,
    )
    assert w1 == [] and t1["evidence_id"] in index["by_span"].values()
    key = next(iter(index["by_span"]))
    # 纪律 1 双向确定性：既有 ID 更小 → 保留既有；更大 → 新票胜出
    index["by_span"][key] = "ev-000000000000"  # 字典序最小（0 是最小 hex 位）
    _, w2 = resolve_or_build_evidence(
        text=_TEXT, quote="违约金为总额百分之三十", parse_source="quality",
        document_version=_DV, index=index,
    )
    assert w2 and w2[0]["reason"] == "duplicate_span"
    assert index["by_span"][key] == "ev-000000000000", "必须收敛到字典序最小"
    index["by_span"][key] = "ev-ffffffffffff"  # 字典序最大
    t3, w3 = resolve_or_build_evidence(
        text=_TEXT, quote="违约金为总额百分之三十", parse_source="fact",
        document_version=_DV, index=index,
    )
    assert w3 and index["by_span"][key] == t3["evidence_id"], "新票更小时新票胜出"


def test_rebuild_index_shape_and_direction():
    """索引方向（第二轮钉 1）：by_span 主键 = 位置→票号；不含原文。"""
    row = normalize_review_evidence({
        "text": _TEXT, "document_version": _DV,
        "items": [{"id": "a", "evidence": build_evidence(
            text=_TEXT, quote="违约金为总额百分之三十", parse_source="rules", document_version=_DV)}],
        "quality": {"observations": [{"id": "b", "evidence": build_evidence(
            text=_TEXT, quote=" 违约金为总额百分之三十。", parse_source="quality", document_version=_DV)}]},
    })
    index = rebuild_evidence_index(row)
    assert index["version"] == 1
    assert len(index["by_span"]) == 1, "同 span 两层票据 → 索引只有一个条目（收敛）"
    key = next(iter(index["by_span"]))
    assert key.startswith(_DV + "|"), "by_span 主键以版本开头（位置→票号方向）"
    assert "违约金" not in json.dumps(index), "缓存不得保存合同原文"


def test_rebuild_never_writes_store():
    """读路径重建只改响应副本——store 行逐字节不动。"""

    sample = Path(__file__).resolve().parents[1] / "fixtures" / "procurement_sample.txt"
    rid = client.post(
        "/api/upload",
        files={"file": ("p.txt", sample.read_bytes(), "text/plain")},
        data={"category": "procurement"},
    ).json()["review_id"]
    wait_review_done(client, rid)
    before = store_module.get(rid)
    snapshot = json.dumps(before, sort_keys=True, ensure_ascii=False)
    client.get(f"/api/review/{rid}")
    client.get(f"/api/review/{rid}")
    after = store_module.get(rid)
    assert json.dumps(after, sort_keys=True, ensure_ascii=False) == snapshot, \
        "GET 不得写 store（2a 红线延续）"


# ---------- pipeline 落库规范化 + 索引持久化 ----------

def test_pipeline_persists_normalized_tickets_and_index():
    """pipeline 落库前规范化（canonical）+ evidence_index 随行持久化——
    读路径零改写，索引可重建。"""

    sample = Path(__file__).resolve().parents[1] / "fixtures" / "procurement_sample.txt"
    rid = client.post(
        "/api/upload",
        files={"file": ("p.txt", sample.read_bytes(), "text/plain")},
        data={"category": "procurement"},
    ).json()["review_id"]
    wait_review_done(client, rid)
    row = store_module.get(rid)
    idx = row.get("evidence_index")
    assert idx and idx["version"] == 1 and isinstance(idx["by_span"], dict), \
        "evidence_index 必须随行持久化"
    # 落库已 canonical：读路径归一化幂等（ticket 内容零变化）
    norm = normalize_review_evidence(row)
    for a, b in zip(row["items"], norm["items"]):
        if a.get("evidence") and b.get("evidence"):
            assert a["evidence"] == b["evidence"], "落库票据应已 canonical（读路径幂等）"
    # 合格票的 quote 必须等于原文切片（canonical 形态）
    text = row.get("text") or ""
    checked = 0
    for it in row["items"]:
        ev = it.get("evidence") or {}
        s, e = ev.get("start"), ev.get("end")
        if ev.get("verification") in ("verified", "ambiguous") and isinstance(s, int):
            assert ev["quote"] == text[s:e][:300], "合格票 quote 必须是原文切片"
            checked += 1
    assert checked >= 1


def test_verify_trigger_merges_index_without_loss():
    """done 后写方（verify 再跑）锁内重建+合并索引——缓存生命周期闭环。"""

    sample = Path(__file__).resolve().parents[1] / "fixtures" / "procurement_sample.txt"
    rid = client.post(
        "/api/upload",
        files={"file": ("p.txt", sample.read_bytes(), "text/plain")},
        data={"category": "procurement"},
    ).json()["review_id"]
    wait_review_done(client, rid)
    before = store_module.get(rid)["evidence_index"]["by_span"]
    r = client.post(f"/api/review/{rid}/verify")
    assert r.status_code == 200
    after = store_module.get(rid)["evidence_index"]["by_span"]
    assert set(before.items()) <= set(after.items()), "再核合并不得丢失既有条目"


def test_registry_detects_duplicate_span_defensively():
    """登记簿 duplicate_span 自检：canonical 收敛后本不应出现同 span 异 ID——
    直接喂未收敛行（绕过 normalize）必须被抓（防御层，直调登记簿）。"""
    ev_a = build_evidence(text=_TEXT, quote="违约金为总额百分之三十", parse_source="rules", document_version=_DV)
    ev_b = dict(ev_a, evidence_id="ev-000000000009")  # 同 span 手塞异 ID
    row = {
        "text": _TEXT, "document_version": _DV,
        "items": [{"id": "a", "evidence": ev_a}],
        "quality": {"observations": [{"id": "b", "evidence": ev_b}]},
    }
    from app.services.evidence import build_evidence_registry
    reg = build_evidence_registry(row, [])
    dup = [w for w in reg["broken_refs"] if w["reason"] == "duplicate_span"]
    assert dup, "同 span 异 ID 必须被登记簿自检抓到"
