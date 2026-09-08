"""upload 预审分支 API 测试（monkeypatch run_precheck，毫秒级、不依赖 LLM）。

对齐小智娘测试计划第 2 部分：confirm 分支无 review_id、确认后重传全流程、
向后兼容、降级照旧开审、预审不污染规则结果。
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.precheck import PrecheckOutcome, PrecheckResult
from tests.helpers import upload_and_wait

ROOT = Path(__file__).resolve().parents[1]
LEASE_FIXTURE = ROOT / "fixtures" / "lease_sample.txt"
PROC_FIXTURE = ROOT / "fixtures" / "procurement_sample.txt"

client = TestClient(app)


def _mock_precheck(monkeypatch, **kw):
    outcome = PrecheckOutcome(performed=True, result=PrecheckResult(**kw))
    monkeypatch.setattr("app.api.routes.precheck_service.run_precheck", lambda t, c: outcome)


def _enable(monkeypatch):
    monkeypatch.setenv("PRECHECK_ENABLED", "true")


# ---------- confirm 分支 ----------

def test_unsupported_branch_no_review_id(monkeypatch):
    _enable(monkeypatch)
    _mock_precheck(
        monkeypatch,
        detected_type="委托创作服务合同",
        is_supported=False,
        suggested_category=None,
        confidence="high",
        summary="以交付成片为核心义务",
    )
    r = client.post(
        "/api/upload",
        files={"file": ("v.txt", LEASE_FIXTURE.read_bytes(), "text/plain")},
        data={"category": "procurement"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "category_confirm"
    assert body["review_id"] is None, "confirm 分支绝不能开审"
    assert body["precheck"]["detected_type"] == "委托创作服务合同"
    assert {c["id"] for c in body["supported_categories"]} == {"lease", "procurement", "nda"}


def test_mismatch_branch_suggests_switch(monkeypatch):
    _enable(monkeypatch)
    _mock_precheck(
        monkeypatch,
        detected_type="房屋租赁合同",
        is_supported=True,
        suggested_category="lease",
        confidence="high",
    )
    r = client.post(
        "/api/upload",
        files={"file": ("l.txt", LEASE_FIXTURE.read_bytes(), "text/plain")},
        data={"category": "procurement"},
    )
    body = r.json()
    assert body["status"] == "category_confirm"
    assert body["suggested_category"] == "lease"
    assert body["review_id"] is None


def test_confirm_then_reupload_full_flow(monkeypatch):
    """确认切换后带正确品类重传 → 正常审查全流程。"""
    _enable(monkeypatch)
    _mock_precheck(
        monkeypatch,
        detected_type="房屋租赁合同",
        is_supported=True,
        suggested_category="lease",
        confidence="high",
    )
    r = client.post(
        "/api/upload",
        files={"file": ("l.txt", LEASE_FIXTURE.read_bytes(), "text/plain")},
        data={"category": "procurement"},
    )
    assert r.json()["status"] == "category_confirm"

    # 用户确认切换：重新 POST，precheck 会再次运行 → 让第二次结果=lease 一致
    _mock_precheck(
        monkeypatch,
        detected_type="房屋租赁合同",
        is_supported=True,
        suggested_category="lease",
        confidence="high",
    )
    review = upload_and_wait(client, str(LEASE_FIXTURE), category="lease")
    assert review["status"] == "done"


# ---------- 向后兼容与降级 ----------

def test_same_category_response_backward_compatible(monkeypatch):
    _enable(monkeypatch)
    _mock_precheck(
        monkeypatch,
        detected_type="采购合同",
        is_supported=True,
        suggested_category="procurement",
        confidence="high",
    )
    review = upload_and_wait(client, str(PROC_FIXTURE), category="procurement")
    assert review["status"] == "done"
    assert review["precheck"] is None, "无倾向差异不产生 suspect 提示"


def test_no_key_degrades_to_normal_upload(monkeypatch):
    """降级矩阵：预审 skip → 照旧开审。"""
    _enable(monkeypatch)
    monkeypatch.setattr(
        "app.api.routes.precheck_service.run_precheck",
        lambda t, c: PrecheckOutcome(skip_reason="no_llm_key"),
    )
    review = upload_and_wait(client, str(PROC_FIXTURE), category="procurement")
    assert review["status"] == "done"


# ---------- suspect 非阻断路径 ----------

def test_low_confidence_mismatch_proceeds_with_suspect_banner(monkeypatch):
    """老钱改判落点：照旧开审 + 记录 suspect，review 与报告都带提示。"""
    _enable(monkeypatch)
    _mock_precheck(
        monkeypatch,
        detected_type="买卖合同",
        is_supported=True,
        suggested_category="procurement",
        confidence="low",
    )
    rid = client.post(
        "/api/upload",
        files={"file": ("p.txt", PROC_FIXTURE.read_bytes(), "text/plain")},
        data={"category": "lease"},
    ).json()["review_id"]
    review = upload_and_wait(client, str(PROC_FIXTURE), category="lease", review_id=rid)
    assert review["status"] == "done"
    assert review["precheck"] is not None
    assert review["precheck"]["suspect"] is True
    assert review["precheck"]["detected_type"] == "买卖合同"

    # 档位不受预审影响：需关注条目的 tag_source 必须全部出自规则
    attention = [i for i in review["items"] if i["status"] == "需关注"]
    assert attention, "夹具应至少命中一条需关注"
    assert all(i.get("tag_source") == "rule" for i in attention)


def test_suspect_line_in_docx_report(monkeypatch):
    _enable(monkeypatch)
    _mock_precheck(
        monkeypatch,
        detected_type="买卖合同",
        is_supported=True,
        suggested_category="procurement",
        confidence="low",
    )
    rid = client.post(
        "/api/upload",
        files={"file": ("p.txt", PROC_FIXTURE.read_bytes(), "text/plain")},
        data={"category": "lease"},
    ).json()["review_id"]
    upload_and_wait(client, str(PROC_FIXTURE), category="lease", review_id=rid)

    import io

    from docx import Document

    r = client.get(f"/api/review/{rid}/report")
    assert r.status_code == 200
    doc = Document(io.BytesIO(r.content))
    text = "\n".join(p.text for p in doc.paragraphs)
    assert "品类存疑" in text
    assert "买卖合同" in text
