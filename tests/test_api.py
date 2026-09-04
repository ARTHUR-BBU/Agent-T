"""Basic API smoke tests (no Grok key required)."""
from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "fixtures" / "procurement_sample.txt"

client = TestClient(app)


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_upload_and_review():
    files = {"file": ("procurement_sample.txt", FIXTURE.read_bytes(), "text/plain")}
    data = {"category": "procurement"}
    r = client.post("/api/upload", files=files, data=data)
    assert r.status_code == 200
    rid = r.json()["review_id"]

    r2 = client.get(f"/api/review/{rid}")
    assert r2.status_code == 200
    body = r2.json()
    assert body["status"] == "done"
    attention = [i for i in body["items"] if i["status"] == "需关注"]
    assert len(attention) >= 7


def test_ask_without_key_clear_message():
    files = {"file": ("procurement_sample.txt", FIXTURE.read_bytes(), "text/plain")}
    rid = client.post("/api/upload", files=files, data={"category": "procurement"}).json()[
        "review_id"
    ]
    review = client.get(f"/api/review/{rid}").json()
    item = next(i for i in review["items"] if i["status"] == "需关注")
    r = client.post(
        "/api/ask",
        json={"review_id": rid, "item_id": item["id"], "question": "风险大吗？"},
    )
    assert r.status_code == 200
    body = r.json()
    # Without API key, should return clear message (ok=False)
    if not body.get("ok"):
        assert "API_KEY" in (body.get("error") or "") or "未配置" in (body.get("error") or "")
