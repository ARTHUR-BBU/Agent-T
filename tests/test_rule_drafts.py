"""Local-only drafts must never mutate effective rules or accept remote writes."""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app
from app.services.checklist import load_checklist


def _client():
    return TestClient(app, base_url="http://127.0.0.1:8000", client=("127.0.0.1", 12345))


def _draft(base_sha256=""):
    return {
        "category": "technical_service", "operation": "add", "item_id": "custom_demo",
        "name": "测试检查项", "description": "检查指定文字", "reason": "本机验证",
        "segment": "B", "literal": "测试关键词", "positive": "包含测试关键词的示例",
        "negative": "不含关键词的示例", "base_sha256": base_sha256,
    }


def test_local_draft_is_isolated_persistent_and_revision_checked(tmp_path, monkeypatch):
    monkeypatch.setenv("RULE_DRAFTS_LOCAL_ENABLED", "1")
    monkeypatch.setenv("RULE_DRAFTS_DB_PATH", str(tmp_path / "drafts.sqlite"))
    original = load_checklist("technical_service")
    with _client() as client:
        headers = {"Origin": "http://127.0.0.1:8000"}
        catalog = client.get("/api/rule-drafts/catalog").json()
        base = next(p["base_sha256"] for p in catalog["packs"] if p["id"] == "technical_service")
        created = client.post("/api/rule-drafts", json=_draft(base), headers=headers)
        assert created.status_code == 200, created.text
        draft = created.json()
        assert draft["status"] == "draft" and draft["validation"]["passed"] is True
        assert len(draft["base_sha256"]) == 64
        payload = {**_draft(base), "name": "新名称", "revision": draft["revision"]}
        updated = client.put(f"/api/rule-drafts/{draft['id']}", json=payload, headers=headers)
        assert updated.status_code == 200
        assert updated.json()["revision"] == 2
        stale = client.put(f"/api/rule-drafts/{draft['id']}", json=payload, headers=headers)
        assert stale.status_code == 409
    with _client() as client:
        listed = client.get("/api/rule-drafts")
        assert listed.status_code == 200
        assert listed.json()["drafts"][0]["name"] == "新名称"
    assert load_checklist("technical_service") == original


def test_draft_access_is_disabled_by_default_and_rejects_cross_origin(tmp_path, monkeypatch):
    monkeypatch.delenv("RULE_DRAFTS_LOCAL_ENABLED", raising=False)
    with _client() as client:
        assert client.get("/api/rule-drafts").status_code == 404
        assert client.get("/api/rule-drafts/capability").json() == {"available": False, "local_only": True}
    monkeypatch.setenv("RULE_DRAFTS_LOCAL_ENABLED", "1")
    monkeypatch.setenv("RULE_DRAFTS_DB_PATH", str(tmp_path / "drafts.sqlite"))
    with _client() as client:
        assert client.post("/api/rule-drafts", json=_draft("0" * 64), headers={"Origin": "https://evil.example"}).status_code == 403
        assert client.post("/api/rule-drafts", json=_draft("0" * 64)).status_code == 403
        assert client.get("/api/rule-drafts", headers={"X-Forwarded-For": "203.0.113.5"}).status_code == 403


def test_new_draft_requires_matching_positive_and_negative_examples(tmp_path, monkeypatch):
    monkeypatch.setenv("RULE_DRAFTS_LOCAL_ENABLED", "1")
    monkeypatch.setenv("RULE_DRAFTS_DB_PATH", str(tmp_path / "drafts.sqlite"))
    with _client() as client:
        catalog = client.get("/api/rule-drafts/catalog").json()
        base = next(p["base_sha256"] for p in catalog["packs"] if p["id"] == "technical_service")
        payload = {**_draft(base), "positive": "没有匹配"}
        response = client.post("/api/rule-drafts", json=payload, headers={"Origin": "http://127.0.0.1:8000"})
    assert response.status_code == 422


def test_literal_special_characters_are_not_executed_as_regex(tmp_path, monkeypatch):
    monkeypatch.setenv("RULE_DRAFTS_LOCAL_ENABLED", "1")
    monkeypatch.setenv("RULE_DRAFTS_DB_PATH", str(tmp_path / "drafts.sqlite"))
    with _client() as client:
        catalog = client.get("/api/rule-drafts/catalog").json()
        base = next(p["base_sha256"] for p in catalog["packs"] if p["id"] == "technical_service")
        payload = {**_draft(base), "literal": "a+b", "positive": "文字 a+b", "negative": "文字 aaab"}
        response = client.post("/api/rule-drafts", json=payload, headers={"Origin": "http://127.0.0.1:8000"})
    assert response.status_code == 200
    assert response.json()["validation"]["passed"] is True


def test_literal_examples_follow_engine_case_insensitivity(tmp_path, monkeypatch):
    monkeypatch.setenv("RULE_DRAFTS_LOCAL_ENABLED", "1")
    monkeypatch.setenv("RULE_DRAFTS_DB_PATH", str(tmp_path / "drafts.sqlite"))
    with _client() as client:
        packs = client.get("/api/rule-drafts/catalog").json()["packs"]
        base = next(p["base_sha256"] for p in packs if p["id"] == "technical_service")
        payload = {**_draft(base), "literal": "abc", "positive": "文字 ABC", "negative": "无此词"}
        response = client.post("/api/rule-drafts", json=payload, headers={"Origin": "http://127.0.0.1:8000"})
        assert response.status_code == 200
        bad = client.post("/api/rule-drafts", json={**payload, "negative": "还有 ABC"},
                          headers={"Origin": "http://127.0.0.1:8000"})
        assert bad.status_code == 422


def test_rejects_hardline_disable_and_wrong_database(tmp_path, monkeypatch):
    from app.api.routes_rule_drafts import _hardline
    from app.services.store import store

    monkeypatch.setenv("RULE_DRAFTS_LOCAL_ENABLED", "1")
    monkeypatch.setenv("RULE_DRAFTS_DB_PATH", str(tmp_path / "drafts.sqlite"))
    with _client() as client:
        catalog = client.get("/api/rule-drafts/catalog").json()
        pack = next(p for p in catalog["packs"] if any(i["has_hardline"] for i in p["items"]))
        item = next(i for i in pack["items"] if i["has_hardline"])
        assert _hardline(load_checklist(pack["id"])["items"][[i["id"] for i in pack["items"]].index(item["id"])])
        payload = {**_draft(pack["base_sha256"]), "category": pack["id"],
                   "operation": "disable", "item_id": item["id"]}
        response = client.post("/api/rule-drafts", json=payload, headers={"Origin": "http://127.0.0.1:8000"})
        assert response.status_code == 422
        response = client.post("/api/rule-drafts", json={**payload, "operation": "edit", "name": "新名称", "description": "新说明"}, headers={"Origin": "http://127.0.0.1:8000"})
        assert response.status_code == 422
        monkeypatch.setenv("RULE_DRAFTS_DB_PATH", store._path)
        assert client.get("/api/rule-drafts").status_code == 503


def test_rejects_stale_baseline_and_can_close_database(tmp_path, monkeypatch):
    from app.services import checklist

    configs = tmp_path / "configs"
    configs.mkdir()
    for source in checklist.CONFIG_DIR.glob("checklist_*.yaml"):
        (configs / source.name).write_bytes(source.read_bytes())
    monkeypatch.setattr(checklist, "CONFIG_DIR", configs)
    monkeypatch.setenv("RULE_DRAFTS_LOCAL_ENABLED", "1")
    db = tmp_path / "drafts.sqlite"
    monkeypatch.setenv("RULE_DRAFTS_DB_PATH", str(db))
    headers = {"Origin": "http://127.0.0.1:8000"}
    with _client() as client:
        packs = client.get("/api/rule-drafts/catalog").json()["packs"]
        base = next(p["base_sha256"] for p in packs if p["id"] == "technical_service")
        created = client.post("/api/rule-drafts", json=_draft(base), headers=headers).json()
        path = configs / "checklist_technical_service.yaml"
        path.write_bytes(path.read_bytes() + b"\n# changed\n")
        response = client.put(f"/api/rule-drafts/{created['id']}",
                              json={**_draft(base), "revision": 1}, headers=headers)
        assert response.status_code == 409
        assert client.delete(f"/api/rule-drafts/{created['id']}?revision=2", headers=headers).status_code == 409
        assert client.delete(f"/api/rule-drafts/{created['id']}?revision=1", headers=headers).status_code == 200
    db.unlink()  # On Windows, succeeds only if request connections were closed.


def test_remote_peer_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("RULE_DRAFTS_LOCAL_ENABLED", "1")
    monkeypatch.setenv("RULE_DRAFTS_DB_PATH", str(tmp_path / "drafts.sqlite"))
    with TestClient(app, base_url="http://127.0.0.1:8000", client=("203.0.113.10", 12345)) as client:
        assert client.get("/api/rule-drafts").status_code == 403


def test_catalog_and_draft_reflect_effective_enabled_flag(tmp_path, monkeypatch):
    import yaml
    from app.services import checklist

    configs = tmp_path / "configs"
    configs.mkdir()
    for source in checklist.CONFIG_DIR.glob("checklist_*.yaml"):
        (configs / source.name).write_bytes(source.read_bytes())
    path = configs / "checklist_procurement.yaml"
    cfg = yaml.safe_load(path.read_bytes())
    target = next(item for item in cfg["items"] if item["id"] == "subject_matter")
    target["enabled"] = False
    path.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    monkeypatch.setattr(checklist, "CONFIG_DIR", configs)
    monkeypatch.setenv("RULE_DRAFTS_LOCAL_ENABLED", "1")
    monkeypatch.setenv("RULE_DRAFTS_DB_PATH", str(tmp_path / "drafts.sqlite"))
    with _client() as client:
        packs = client.get("/api/rule-drafts/catalog").json()["packs"]
        pack = next(p for p in packs if p["id"] == "procurement")
        item = next(i for i in pack["items"] if i["id"] == "subject_matter")
        assert item["enabled"] is False
        payload = {**_draft(pack["base_sha256"]), "category": "procurement",
                   "operation": "enable", "item_id": item["id"]}
        response = client.post("/api/rule-drafts", json=payload, headers={"Origin": "http://127.0.0.1:8000"})
        assert response.status_code == 200
        assert response.json()["before"]["enabled"] is False
        assert response.json()["after"]["enabled"] is True
        active = next(i for i in pack["items"] if i["enabled"])
        payload = {**_draft(pack["base_sha256"]), "category": "procurement", "operation": "enable", "item_id": active["id"]}
        response = client.post("/api/rule-drafts", json=payload, headers={"Origin": "http://127.0.0.1:8000"})
        assert response.status_code == 422


def test_edit_and_disable_are_only_proposals(tmp_path, monkeypatch):
    monkeypatch.setenv("RULE_DRAFTS_LOCAL_ENABLED", "1")
    monkeypatch.setenv("RULE_DRAFTS_DB_PATH", str(tmp_path / "drafts.sqlite"))
    original = load_checklist("technical_service")
    headers = {"Origin": "http://127.0.0.1:8000"}
    with _client() as client:
        packs = client.get("/api/rule-drafts/catalog").json()["packs"]
        pack = next(p for p in packs if p["id"] == "technical_service")
        item = next(i for i in pack["items"] if not i["has_hardline"])
        payload = {**_draft(pack["base_sha256"]), "item_id": item["id"], "operation": "edit",
                   "name": "建议的新名称", "description": "建议的新说明"}
        edited = client.post("/api/rule-drafts", json=payload, headers=headers)
        assert edited.status_code == 200
        assert edited.json()["before"]["name"] == item["name"]
        assert edited.json()["after"]["name"] == "建议的新名称"
        assert edited.json()["validation"]["passed"] is False
        disabled = client.post("/api/rule-drafts", json={**payload, "operation": "disable"}, headers=headers)
        assert disabled.status_code == 200
        assert disabled.json()["before"]["enabled"] is True
        assert disabled.json()["after"]["enabled"] is False
    assert load_checklist("technical_service") == original


def test_concurrent_updates_do_not_overwrite_each_other(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading

    monkeypatch.setenv("RULE_DRAFTS_LOCAL_ENABLED", "1")
    monkeypatch.setenv("RULE_DRAFTS_DB_PATH", str(tmp_path / "drafts.sqlite"))
    headers = {"Origin": "http://127.0.0.1:8000"}
    with _client() as client:
        packs = client.get("/api/rule-drafts/catalog").json()["packs"]
        base = next(p["base_sha256"] for p in packs if p["id"] == "technical_service")
        created = client.post("/api/rule-drafts", json=_draft(base), headers=headers).json()
    barrier = threading.Barrier(2)

    def update(name):
        with _client() as client:
            barrier.wait(timeout=10)
            return client.put(f"/api/rule-drafts/{created['id']}",
                              json={**_draft(base), "revision": 1, "name": name}, headers=headers).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(update, ["修改甲", "修改乙"]))
    assert sorted(statuses) == [200, 409]
