"""规则提案评审：记录意见不发布规则，且不越过审查记录的保留期。"""
from tests.test_objection_api import _make_done_with_objections, client, store_module


def test_proposal_review_is_traceable_and_does_not_change_rules():
    rid = _make_done_with_objections()
    before = store_module.get(rid)["items"]
    assert client.post(f"/api/review/{rid}/objections/adopt", json={"index": 0}).status_code == 200
    proposals = client.get("/api/objection-proposals").json()["proposals"]
    proposal = next(p for p in proposals if p["review_id"] == rid)
    data = {"claim_id": proposal["claim_id"], "claim_content_hash": proposal["claim_content_hash"],
            "snapshot_hash": proposal["snapshot_hash"], "revision": 0, "reviewer": "审核人", "note": "需要补充验证", "verdict": "approved"}
    response = client.post(f"/api/review/{rid}/objection-proposals/review", json=data)
    assert response.status_code == 200
    assert response.json()["rule_published"] is False
    assert store_module.get(rid)["items"] == before
    again = next(p for p in client.get("/api/objection-proposals").json()["proposals"] if p["review_id"] == rid)
    assert again["reviews"][0]["note"] == "需要补充验证"
    assert client.post(f"/api/review/{rid}/objection-proposals/review", json=data).status_code == 409
    data["revision"] = 1
    data["claim_content_hash"] = "stale"
    assert client.post(f"/api/review/{rid}/objection-proposals/review", json=data).status_code == 409


def test_proposal_review_requires_real_text_and_adoption():
    rid = _make_done_with_objections()
    response = client.post(f"/api/review/{rid}/objection-proposals/review", json={
        "claim_id": "x", "claim_content_hash": "x", "snapshot_hash": "x" * 64, "revision": 0,
        "reviewer": " ", "note": "x", "verdict": "approved"})
    assert response.status_code == 422
    assert all(p["review_id"] != rid for p in client.get("/api/objection-proposals").json()["proposals"])


def test_readoption_preserves_review_history():
    rid = _make_done_with_objections()
    assert client.post(f"/api/review/{rid}/objections/adopt", json={"index": 0}).status_code == 200
    proposal = next(p for p in client.get("/api/objection-proposals").json()["proposals"] if p["review_id"] == rid)
    data = {"claim_id": proposal["claim_id"], "claim_content_hash": proposal["claim_content_hash"],
            "snapshot_hash": proposal["snapshot_hash"], "revision": 0, "reviewer": "甲", "note": "复核", "verdict": "rejected"}
    assert client.post(f"/api/review/{rid}/objection-proposals/review", json=data).status_code == 200
    assert client.post(f"/api/review/{rid}/objections/adopt", json={"index": 0}).status_code == 200
    item = next(p for p in client.get("/api/objection-proposals").json()["proposals"] if p["review_id"] == rid)
    assert item["reviews"][0]["verdict"] == "rejected"


def test_changed_proposal_does_not_inherit_prior_review():
    rid = _make_done_with_objections()
    assert client.post(f"/api/review/{rid}/objections/adopt", json={"index": 0}).status_code == 200
    original = next(p for p in client.get("/api/objection-proposals").json()["proposals"] if p["review_id"] == rid)
    data = {"claim_id": original["claim_id"], "claim_content_hash": original["claim_content_hash"],
            "snapshot_hash": original["snapshot_hash"], "revision": 0, "reviewer": "甲", "note": "同意旧版", "verdict": "approved"}
    assert client.post(f"/api/review/{rid}/objection-proposals/review", json=data).status_code == 200
    def change(row):
        row["objections"]["objections"][0]["proposal"] = "内容发生变化，需要重新评审"
    store_module.mutate(rid, change)
    assert all(p["review_id"] != rid for p in client.get("/api/objection-proposals").json()["proposals"])
    assert client.post(f"/api/review/{rid}/objections/adopt", json={"index": 0}).status_code == 200
    new = next(p for p in client.get("/api/objection-proposals").json()["proposals"] if p["review_id"] == rid)
    assert new["claim_content_hash"] != original["claim_content_hash"]
    assert new["reviews"][0]["claim_content_hash"] == original["claim_content_hash"]


def test_proposal_disappears_with_expired_review():
    import time
    rid = _make_done_with_objections()
    assert client.post(f"/api/review/{rid}/objections/adopt", json={"index": 0}).status_code == 200
    with store_module._conn() as conn:
        conn.execute("UPDATE reviews SET created_at = ? WHERE id = ?", (time.time() - 1000000, rid))
    assert all(p["review_id"] != rid for p in client.get("/api/objection-proposals").json()["proposals"])


def test_changed_counter_evidence_requires_new_review():
    rid = _make_done_with_objections()
    assert client.post(f"/api/review/{rid}/objections/adopt", json={"index": 0}).status_code == 200
    old = next(p for p in client.get("/api/objection-proposals").json()["proposals"] if p["review_id"] == rid)
    data = {"claim_id": old["claim_id"], "claim_content_hash": old["claim_content_hash"],
            "snapshot_hash": old["snapshot_hash"], "revision": 0, "reviewer": "甲",
            "note": "旧反证下同意", "verdict": "approved"}
    assert client.post(f"/api/review/{rid}/objection-proposals/review", json=data).status_code == 200
    def change(row):
        row["objections"]["objections"][0]["counter_evidence"] = "变更的反证说明"
    store_module.mutate(rid, change)
    assert client.post(f"/api/review/{rid}/objections/adopt", json={"index": 0}).status_code == 200
    new = next(p for p in client.get("/api/objection-proposals").json()["proposals"] if p["review_id"] == rid)
    assert new["claim_id"] == old["claim_id"]
    assert new["claim_content_hash"] == old["claim_content_hash"]
    assert new["snapshot_hash"] != old["snapshot_hash"]
    assert new["reviews"][0]["snapshot_hash"] != new["snapshot_hash"]
    data["revision"] = 1
    assert client.post(f"/api/review/{rid}/objection-proposals/review", json=data).status_code == 409
