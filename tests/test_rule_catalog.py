"""The catalog exposes only the currently available checklist, without writes."""
from fastapi.testclient import TestClient

from app.main import app
from app.api import routes
from app.services.checklist import list_categories, load_checklist, run_checklist


def test_catalog_matches_visible_checklists():
    response = TestClient(app).get("/api/rules/catalog")
    assert response.status_code == 200
    packs = response.json()["packs"]
    assert {pack["id"] for pack in packs} == {c["id"] for c in list_categories()}
    for pack in packs:
        cfg = load_checklist(pack["id"])
        assert [(i["id"], i["name"]) for i in pack["items"]] == [
            (i["id"], i["name"]) for i in cfg["items"]
        ]
        assert [i["definition"] for i in pack["items"]] == cfg["items"]


def test_catalog_omits_unapproved_fields_at_every_level(monkeypatch):
    monkeypatch.setattr(routes, "list_categories", lambda: [{"id": "example", "label": "示例"}])
    monkeypatch.setattr(routes, "load_checklist", lambda category: {"items": [{
        "id": "one", "name": "示例", "private_metadata": "secret",
        "rules": {"pass": [{"pattern": "a", "private_metadata": "secret"}]},
    }]})
    response = TestClient(app).get("/api/rules/catalog")
    assert response.status_code == 200
    assert "private_metadata" not in response.text
    assert response.json()["packs"][0]["items"][0]["definition"]["rules"]["pass"][0]["pattern"] == "a"


def test_catalog_capability_does_not_include_rules():
    response = TestClient(app).get("/api/rules/catalog/capability")
    assert response.status_code == 200
    assert response.json() == {"available": True}


def test_technical_service_missing_notes_are_factually_neutral():
    items = run_checklist("技术服务合同。甲方委托乙方维护系统。", category="technical_service")["items"]
    items = {item["id"]: item for item in items}
    assert items["subject"]["status"] == "需关注"
    assert "仅见公司名称" not in items["subject"]["note"]
    assert items["acceptance"]["status"] == "需关注"
    assert "技术开发合同" not in items["acceptance"]["note"]
