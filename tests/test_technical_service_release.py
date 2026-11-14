"""技术服务开放：品类、立场、预审白名单与实际上传链路。"""
from fastapi.testclient import TestClient
import pytest

from app.main import app
from app.prompts.precheck import build_system_prompt
from app.services import checklist, precheck, stance
from tests.helpers import wait_review_done


def test_technical_service_is_visible_with_three_stances():
    categories = checklist.list_categories()
    tech = next(c for c in categories if c["id"] == "technical_service")
    assert tech["stances"]["allowed"] == ["neutral", "client", "vendor"]
    for value in ("neutral", "client", "vendor"):
        assert stance.is_allowed("technical_service", value)
        assert "核查口径不因立场改变" in stance.declaration("technical_service", value)


def test_precheck_prompt_and_whitelist_include_technical_service():
    prompt = build_system_prompt(checklist.list_categories())
    assert "技术服务合同（technical_service）" in prompt
    assert "技术转让或许可" in prompt and "品牌推广" in prompt and "研发新技术成果" in prompt
    assert "lease/procurement/nda 之一" not in prompt
    result = precheck._parse_payload('{"detected_type":"技术服务合同","is_supported":true,'
                                     '"suggested_category":"technical_service","confidence":"high",'
                                     '"summary":"技术服务"}')
    assert result and result.suggested_category == "technical_service"
    assert precheck.decide_branch(precheck.PrecheckOutcome(performed=True, result=result), "lease")["action"] == "confirm_switch"


@pytest.mark.parametrize(("selected_stance", "expected"), [
    ("neutral", "未声明代表方"), ("client", "委托方"), ("vendor", "受托方"),
])
def test_upload_technical_service_runs_thirteen_items_without_live_model(monkeypatch, selected_stance, expected):
    monkeypatch.setenv("PRECHECK_ENABLED", "false")
    monkeypatch.setenv("CATEGORY_SUGGEST_ENABLED", "false")
    client = TestClient(app)
    upload = client.post("/api/upload", files={"file": ("tech.txt", "技术服务合同\n甲方委托乙方提供软件维护服务。".encode(), "text/plain")},
                         data={"category": "technical_service", "stance": selected_stance})
    assert upload.status_code == 200
    review = wait_review_done(client, upload.json()["review_id"])
    assert review["status"] == "done"
    assert review["category"] == "technical_service"
    assert len(review["items"]) == 13
    assert expected in review["stance_declaration"]


def test_precheck_suggests_technical_service_without_silent_switch(monkeypatch):
    from app.services.precheck import PrecheckOutcome, PrecheckResult
    monkeypatch.setenv("PRECHECK_ENABLED", "true")
    monkeypatch.setenv("CATEGORY_SUGGEST_ENABLED", "false")
    outcome = PrecheckOutcome(performed=True, result=PrecheckResult(
        detected_type="技术服务合同", is_supported=True,
        suggested_category="technical_service", confidence="high", summary="技术服务内容"))
    monkeypatch.setattr("app.api.routes.precheck_service.run_precheck", lambda *a, **kw: outcome)
    client = TestClient(app)
    response = client.post("/api/upload", files={"file": ("tech.txt", "技术服务测试合同".encode(), "text/plain")},
                           data={"category": "lease", "stance": "neutral"})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "category_confirm"
    assert body["suggested_category"] == "technical_service"
    assert body["review_id"] is None
