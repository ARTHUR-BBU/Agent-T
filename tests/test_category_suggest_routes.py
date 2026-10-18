# -*- coding: utf-8 -*-
"""B2-3 批3：上传路径快思考建议层集成测试（五态路由）。

开关关=行为与现状完全一致（零请求）；开关开+不一致=category_confirm
（置信度只改文案）；other 兜底与 force 重传放行。
"""
from __future__ import annotations

from unittest import mock

from fastapi.testclient import TestClient

from app.main import app
from app.services import category_suggest as cs
from app.services import precheck as precheck_service

client = TestClient(app)

FIXTURE_TXT = b"\xe6\x8a\x80\xe6\x9c\xaf\xe6\x9c\x8d\xe5\x8a\xa1\xe5\x90\x88\xe5\x90\x8c\n\xe7\x94\xb2\xe6\x96\xb9\xe5\xa7\x94\xe6\x89\x98\xe4\xb9\x99\xe6\x96\xb9\xe5\x81\x9a\xe7\xb3\xbb\xe7\xbb\x9f\xe3\x80\x82"  # 技术服务合同样例


def _suggestion(category: str, confidence: float) -> cs.SuggestOutcome:
    return cs.SuggestOutcome(
        performed=True, skip_reason=None,
        coverage={"coverage": "full"},
        result=cs.Suggestion(category=category, confidence=confidence),
        provider="glm", model_version="glm-4-flash",
        latency_s=0.5, cost_note="in=1 out=1", raw_output="{}")


def _upload(category: str = "procurement", force: bool = False):
    files = {"file": ("c.txt", FIXTURE_TXT, "text/plain")}
    data = {"category": category, "force": str(force).lower()}
    return client.post("/api/upload", files=files, data=data)


def test_switch_off_default_no_suggestion(monkeypatch) -> None:
    """开关默认关：建议层完全不执行（mock 一旦被碰即炸），行为与现状一致。"""
    monkeypatch.delenv("CATEGORY_SUGGEST_ENABLED", raising=False)
    called = mock.Mock(side_effect=AssertionError("开关关仍执行建议层"))
    with mock.patch.object(cs, "suggest_category", called):
        r = _upload()
    assert r.status_code == 200
    assert r.json()["status"] == "uploaded"
    called.assert_not_called()


def test_agree_goes_straight(monkeypatch) -> None:
    """建议与用户一致→照常 uploaded（无感）。"""
    monkeypatch.setenv("CATEGORY_SUGGEST_ENABLED", "true")
    with mock.patch.object(cs, "suggest_category",
                           return_value=_suggestion("lease", 1.0)), \
            mock.patch.object(precheck_service, "is_precheck_enabled",
                              return_value=False):
        r = _upload(category="lease")
    assert r.status_code == 200
    assert r.json()["status"] == "uploaded"


def test_disagree_always_confirms(monkeypatch) -> None:
    """不一致一律 category_confirm——高置信（≥0.90）也不例外。"""
    monkeypatch.setenv("CATEGORY_SUGGEST_ENABLED", "true")
    with mock.patch.object(cs, "suggest_category",
                           return_value=_suggestion("lease", 1.0)), \
            mock.patch.object(precheck_service, "is_precheck_enabled",
                              return_value=False):
        r = _upload(category="procurement")
    body = r.json()
    assert body["status"] == "category_confirm"
    assert body["suggested_category"] == "lease"
    assert body["precheck"]["confidence"] == "high"
    # 老钱话术红线：不出现「风险/不合规」
    assert "风险" not in body["precheck"]["summary"]
    assert "不合规" not in body["precheck"]["summary"]


def test_disagree_low_confidence_still_confirms(monkeypatch) -> None:
    """低置信（<0.90）不一致同样弹窗——置信度只改文案（「把握较低」）。"""
    monkeypatch.setenv("CATEGORY_SUGGEST_ENABLED", "true")
    with mock.patch.object(cs, "suggest_category",
                           return_value=_suggestion("lease", 0.74)), \
            mock.patch.object(precheck_service, "is_precheck_enabled",
                              return_value=False):
        r = _upload(category="procurement")
    body = r.json()
    assert body["status"] == "category_confirm"
    assert body["precheck"]["confidence"] == "low"
    assert "把握较低" in body["precheck"]["summary"]


def test_other_suggestion_passes_through(monkeypatch) -> None:
    """other 兜底→照常审查（不拦、不弹）。"""
    monkeypatch.setenv("CATEGORY_SUGGEST_ENABLED", "true")
    with mock.patch.object(cs, "suggest_category",
                           return_value=_suggestion("other", 1.0)), \
            mock.patch.object(precheck_service, "is_precheck_enabled",
                              return_value=False):
        r = _upload()
    assert r.json()["status"] == "uploaded"


def test_force_skips_suggestion_confirm(monkeypatch) -> None:
    """force 重传（用户已在确认弹窗拍板）→跳过建议层确认，直接开审。"""
    monkeypatch.setenv("CATEGORY_SUGGEST_ENABLED", "true")
    called = mock.Mock(side_effect=AssertionError("force 仍执行建议层"))
    with mock.patch.object(cs, "suggest_category", called), \
            mock.patch.object(precheck_service, "is_precheck_enabled",
                              return_value=False):
        r = _upload(force=True)
    assert r.status_code == 200
    assert r.json()["status"] == "uploaded"
    called.assert_not_called()


def test_unsupported_suggestion_reject_wording(monkeypatch) -> None:
    """小智娘 P1-2：unsupported 建议（consulting）=主场景——弹窗文案必须
    是老钱拒收话术（暂不在支持范围），suggested_category=None（防 422）。"""
    monkeypatch.setenv("CATEGORY_SUGGEST_ENABLED", "true")
    with mock.patch.object(cs, "suggest_category",
                           return_value=_suggestion("consulting", 1.0)), \
            mock.patch.object(precheck_service, "is_precheck_enabled",
                              return_value=False):
        r = _upload()
    body = r.json()
    assert body["status"] == "category_confirm"
    assert body["suggested_category"] is None, "unsupported 建议不得透传（防 422）"
    assert "暂不在支持范围" in body["precheck"]["summary"]
    assert "风险" not in body["precheck"]["summary"]
    assert body["precheck"]["detected_type"] == "咨询合同"


def test_cross_supported_disagree_no_english_id(monkeypatch) -> None:
    """小智娘 P2-1：跨支持类不一致（procurement vs lease）文案不露英文 id。"""
    monkeypatch.setenv("CATEGORY_SUGGEST_ENABLED", "true")
    with mock.patch.object(cs, "suggest_category",
                           return_value=_suggestion("lease", 0.95)), \
            mock.patch.object(precheck_service, "is_precheck_enabled",
                              return_value=False):
        r = _upload(category="procurement")
    body = r.json()
    assert body["status"] == "category_confirm"
    assert body["suggested_category"] == "lease"
    assert body["precheck"]["detected_type"] == "租赁合同"
    assert "lease" not in body["precheck"]["summary"]
    assert "lease" not in body["precheck"]["detected_type"]
