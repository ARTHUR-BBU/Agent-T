"""Snapshots must keep validated configuration bytes stable across changes."""
from __future__ import annotations

import hashlib

import pytest

from app.services import checklist
from app.services.rule_snapshot import RuleSnapshot, capture_rule_snapshot


def test_snapshot_remains_stable_when_file_changes(tmp_path, monkeypatch):
    source = checklist.CONFIG_DIR / "checklist_technical_service.yaml"
    path = tmp_path / source.name
    original = source.read_bytes()
    path.write_bytes(original)
    monkeypatch.setattr(checklist, "CONFIG_DIR", tmp_path)

    first = capture_rule_snapshot("technical_service")
    edited = original.replace("技术服务".encode(), "服务测试".encode(), 1)
    path.write_bytes(edited)
    second = capture_rule_snapshot("technical_service")

    assert first.config_bytes == original
    assert first.content_version == hashlib.sha256(original).hexdigest()[:12]
    assert second.content_version == hashlib.sha256(edited).hexdigest()[:12]
    assert first.content_version != second.content_version
    config = first.configuration()
    config["items"].clear()
    assert len(first.configuration()["items"]) == 13


def test_snapshot_rejects_unknown_matchers(tmp_path, monkeypatch):
    source = checklist.CONFIG_DIR / "checklist_technical_service.yaml"
    path = tmp_path / source.name
    raw = source.read_text(encoding="utf-8")
    raw = raw.replace("rules:\n      need_attention:",
                      "rules:\n      need_attention:\n        - matcher: unknown_matcher", 1)
    path.write_text(raw, encoding="utf-8")
    monkeypatch.setattr(checklist, "CONFIG_DIR", tmp_path)
    with pytest.raises(ValueError, match="unknown_matcher"):
        capture_rule_snapshot("technical_service")


def test_snapshot_never_falls_back_to_other_contract_type(tmp_path, monkeypatch):
    source = checklist.CONFIG_DIR / "checklist_procurement.yaml"
    (tmp_path / source.name).write_bytes(source.read_bytes())
    monkeypatch.setattr(checklist, "CONFIG_DIR", tmp_path)
    with pytest.raises(FileNotFoundError):
        capture_rule_snapshot("technical_service")


def test_direct_snapshot_construction_rejects_forged_hash():
    raw = (checklist.CONFIG_DIR / "checklist_technical_service.yaml").read_bytes()
    with pytest.raises(ValueError, match="does not match"):
        RuleSnapshot("technical_service", raw, "not-the-hash")


def test_disabled_non_hardline_item_is_excluded_and_can_be_enabled():
    import yaml

    snapshot = capture_rule_snapshot("procurement")
    config = snapshot.configuration()
    item = next(i for i in config["items"] if i["id"] == "subject_matter")
    item["enabled"] = False
    raw = yaml.safe_dump(config, allow_unicode=True).encode()
    disabled = RuleSnapshot("procurement", raw, hashlib.sha256(raw).hexdigest()[:12])
    ids = [i["id"] for i in checklist.run_checklist("甲方", configuration=disabled.configuration())["items"]]
    assert "subject_matter" not in ids
    item["enabled"] = True
    raw = yaml.safe_dump(config, allow_unicode=True).encode()
    enabled = RuleSnapshot("procurement", raw, hashlib.sha256(raw).hexdigest()[:12])
    assert "subject_matter" in [i["id"] for i in checklist.run_checklist("甲方", configuration=enabled.configuration())["items"]]


def test_snapshot_rejects_disabled_hardline_and_invalid_enabled_values():
    import yaml

    snapshot = capture_rule_snapshot("procurement")
    config = snapshot.configuration()
    hardline = next(i for i in config["items"] if "hardline" in str(i))
    hardline["enabled"] = False
    raw = yaml.safe_dump(config, allow_unicode=True).encode()
    with pytest.raises(ValueError, match="硬性"):
        RuleSnapshot("procurement", raw, hashlib.sha256(raw).hexdigest()[:12])
    hardline["enabled"] = "false"
    raw = yaml.safe_dump(config, allow_unicode=True).encode()
    with pytest.raises(ValueError, match="enabled"):
        RuleSnapshot("procurement", raw, hashlib.sha256(raw).hexdigest()[:12])


def test_cannot_disable_last_item_in_a_score_segment():
    import yaml

    config = capture_rule_snapshot("procurement").configuration()
    subject = next(i for i in config["items"] if i["id"] == "subject")
    subject["enabled"] = False
    raw = yaml.safe_dump(config, allow_unicode=True).encode()
    with pytest.raises(ValueError, match="评分分段"):
        RuleSnapshot("procurement", raw, hashlib.sha256(raw).hexdigest()[:12])


def test_cannot_leave_only_na_or_conditional_items_in_disabled_segment():
    import yaml

    config = capture_rule_snapshot("lease").configuration()
    target = next(i for i in config["items"] if i["id"] == "jurisdiction")
    target["enabled"] = False
    raw = yaml.safe_dump(config, allow_unicode=True).encode()
    with pytest.raises(ValueError, match="评分分段"):
        RuleSnapshot("lease", raw, hashlib.sha256(raw).hexdigest()[:12])


def test_pipeline_uses_disabled_state_from_captured_snapshot(monkeypatch):
    import yaml
    from app.graph import pipeline

    baseline = capture_rule_snapshot("procurement")
    config = baseline.configuration()
    next(i for i in config["items"] if i["id"] == "subject_matter")["enabled"] = False
    raw = yaml.safe_dump(config, allow_unicode=True).encode()
    disabled = RuleSnapshot("procurement", raw, hashlib.sha256(raw).hexdigest()[:12])
    monkeypatch.setenv("QUALITY_ENABLED", "false")
    monkeypatch.setenv("OBJECTIONS_ENABLED", "false")
    pipeline.reset_graph()
    old_result = pipeline.run_review("test.txt", "采购合同 规格参数".encode(), rule_snapshot=baseline)
    new_result = pipeline.run_review("test.txt", "采购合同 规格参数".encode(), rule_snapshot=disabled)
    assert "subject_matter" in {i["id"] for i in old_result["items"]}
    assert "subject_matter" not in {i["id"] for i in new_result["items"]}
    assert old_result["rule_pack"]["rule_pack_content_version"] == baseline.content_version
    assert new_result["rule_pack"]["rule_pack_content_version"] == disabled.content_version


@pytest.mark.parametrize("content", [
    "category: technical_service\n",
    "category: technical_service\nitems: []\n",
    "category: technical_service\nitems:\n  - id: x\n",
    "category: technical_service\nitems:\n  - id: x\n    name: x\n    rules: {}\n",
    "category: technical_service\nscorecard: [bad]\nitems:\n  - id: x\n    name: x\n    na: true\n",
    "category: technical_service\nstances:\n  labels: [bad]\nitems:\n  - id: x\n    name: x\n    na: true\n",
    "category: technical_service\nscorecard:\n  segments:\n    - key: A\n      weight: bad\nitems:\n  - id: x\n    name: x\n    na: true\n    segment: A\n",
])
def test_snapshot_rejects_incomplete_config(tmp_path, monkeypatch, content):
    (tmp_path / "checklist_technical_service.yaml").write_text(content, encoding="utf-8")
    monkeypatch.setattr(checklist, "CONFIG_DIR", tmp_path)
    with pytest.raises(ValueError):
        capture_rule_snapshot("technical_service")


@pytest.mark.parametrize("bad_rule", [
    {"any_of": 123},
    {"matcher": []},
    {"matcher": {}},
    {"hit_alternatives": ["bad"]},
    {"hit_alternatives": [{"pattern": "[", "start_token": "x", "neg_sensitive": True}]},
    {"hit_alternatives": [{"pattern": "(x)", "start_token": "x", "neg_sensitive": False},
                          {"pattern": "y", "start_token": "y", "neg_sensitive": False}]},
])
def test_snapshot_rejects_invalid_nested_rule_shapes(tmp_path, monkeypatch, bad_rule):
    import yaml

    source = checklist.CONFIG_DIR / "checklist_technical_service.yaml"
    cfg = yaml.safe_load(source.read_bytes())
    cfg["items"][0]["rules"] = {"pass": [bad_rule]}
    (tmp_path / source.name).write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    monkeypatch.setattr(checklist, "CONFIG_DIR", tmp_path)
    with pytest.raises(ValueError):
        capture_rule_snapshot("technical_service")


def test_review_uses_same_snapshot_after_file_changes(tmp_path, monkeypatch):
    """The next review sees B; the in-flight review keeps A through scoring."""
    from app.graph import pipeline
    from app.services import model_review, scorecard

    source = checklist.CONFIG_DIR / "checklist_technical_service.yaml"
    path = tmp_path / source.name
    original = source.read_bytes()
    edited = original.replace("主体资格".encode(), "主体资格新版".encode(), 1)
    edited = edited.replace("已见主体名称".encode(), "新版已见主体名称".encode(), 1)
    edited = edited.replace("client: 委托方".encode(), "client: 新版委托方".encode(), 1)
    path.write_bytes(original)
    monkeypatch.setattr(checklist, "CONFIG_DIR", tmp_path)
    monkeypatch.setenv("QUALITY_ENABLED", "false")
    monkeypatch.setenv("OBJECTIONS_ENABLED", "false")
    observed_segments = []
    partial_versions = []
    quality_labels = []
    original_prompt = scorecard.build_system_prompt
    original_quality = pipeline.quality_service.run_quality

    def capture_prompt(segments, policies):
        observed_segments.append(segments)
        return original_prompt(segments, policies)

    def mock_model(**kwargs):
        return model_review.run_model_review(**kwargs, chat_fn=lambda *_: "{}")

    def capture_quality(**kwargs):
        quality_labels.append(kwargs["stance_label"])
        return original_quality(**kwargs)

    def update_after_rules(completion, fields):
        if completion == "rules_complete":
            partial_versions.append(fields["rule_pack"]["rule_pack_content_version"])
            path.write_bytes(edited)

    monkeypatch.setattr(scorecard, "build_system_prompt", capture_prompt)
    monkeypatch.setattr(pipeline, "run_model_review", mock_model)
    monkeypatch.setattr(pipeline.quality_service, "run_quality", capture_quality)
    pipeline.reset_graph()
    first = pipeline.run_review("a.txt", "技术服务合同，甲方委托乙方提供服务。".encode(),
                                category="technical_service", stance="client", on_partial=update_after_rules)
    assert observed_segments[0][0]["name"] == "主体资格"
    assert "新版已见主体名称" not in next(i for i in first["items"] if i["id"] == "subject")["note"]
    assert partial_versions == [hashlib.sha256(original).hexdigest()[:12]]
    assert first["rule_pack"]["rule_pack_content_version"] == hashlib.sha256(original).hexdigest()[:12]
    observed_segments.clear()
    second = pipeline.run_review("b.txt", "技术服务合同，甲方委托乙方提供服务。".encode(),
                                 category="technical_service", stance="client")
    assert observed_segments[0][0]["name"] == "主体资格新版"
    assert "新版已见主体名称" in next(i for i in second["items"] if i["id"] == "subject")["note"]
    assert second["rule_pack"]["rule_pack_content_version"] == hashlib.sha256(edited).hexdigest()[:12]
    assert quality_labels == ["委托方", "新版委托方"]


def test_quality_and_ask_prompts_use_saved_label_without_loading_current_rules(monkeypatch):
    from app.prompts import quality
    from app.services import llm_ask, stance

    def unexpected(*args, **kwargs):
        raise AssertionError("saved label must not reload current configuration")

    monkeypatch.setattr(stance, "stance_label", unexpected)
    assert "历史委托方" in quality.build_system_prompt([], category="technical_service", stance="client", stance_label="历史委托方")
    assert "历史委托方" in quality.build_user_prompt("正文", [], category="technical_service", stance="client", stance_label="历史委托方")
    assert "历史委托方" in llm_ask._build_system_prompt([], category="technical_service", stance="client", stance_label="历史委托方")
    assert "历史委托方" in llm_ask._build_user_prompt("问题", {}, "正文", category="technical_service", stance="client", stance_label="历史委托方")


def test_rule_evaluation_and_version_do_not_reread_after_capture(tmp_path, monkeypatch):
    from app.graph import pipeline

    source = checklist.CONFIG_DIR / "checklist_technical_service.yaml"
    path = tmp_path / source.name
    raw = source.read_bytes()
    path.write_bytes(raw)
    monkeypatch.setattr(checklist, "CONFIG_DIR", tmp_path)
    original_capture = pipeline.capture_rule_snapshot

    def capture_then_remove(category):
        snapshot = original_capture(category)
        path.unlink()
        return snapshot

    monkeypatch.setattr(pipeline, "capture_rule_snapshot", capture_then_remove)
    pipeline.reset_graph()
    result = pipeline.run_review("a.txt", "技术服务合同。".encode(), category="technical_service")
    assert result["rule_pack"]["rule_pack_content_version"] == hashlib.sha256(raw).hexdigest()[:12]
    assert len(result["items"]) == 13


def test_upload_keeps_the_snapshot_checked_before_worker(tmp_path, monkeypatch):
    import io
    from docx import Document
    from fastapi.testclient import TestClient
    from app.api import routes
    from app.main import app
    from app.services.store import ReviewStore
    from tests.helpers import wait_review_done

    source = checklist.CONFIG_DIR
    for path in source.glob("checklist_*.yaml"):
        (tmp_path / path.name).write_bytes(path.read_bytes())
    target = tmp_path / "checklist_technical_service.yaml"
    original = target.read_bytes()
    edited = original.replace("已见主体名称".encode(), "新版已见主体名称".encode(), 1)
    edited = edited.replace("client: 委托方".encode(), "client: 新版委托方".encode(), 1)
    monkeypatch.setattr(checklist, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(routes, "store", ReviewStore(str(tmp_path / "isolated.sqlite")))
    real_list_categories = routes.list_categories

    def stale_category_list():
        return [{**item, "label": "旧版分类名称"} if item["id"] == "technical_service" else item
                for item in real_list_categories()]

    monkeypatch.setattr(routes, "list_categories", stale_category_list)
    original_capture = capture_rule_snapshot
    precheck_args = []

    def capture_precheck(*args):
        from app.services.precheck import PrecheckOutcome
        precheck_args.append(args)
        return PrecheckOutcome(skip_reason="disabled")

    monkeypatch.setattr(routes.precheck_service, "run_precheck", capture_precheck)

    def capture_then_change(category):
        snapshot = original_capture(category)
        target.write_bytes(edited)
        return snapshot

    monkeypatch.setattr(routes, "capture_rule_snapshot", capture_then_change)
    with TestClient(app) as client:
        response = client.post("/api/upload", data={
            "category": "technical_service", "stance": "client",
            "pasted_text": "技术服务合同。甲方委托乙方维护系统。",
        })
        assert response.status_code == 200
        review_id = response.json()["review_id"]
        review = wait_review_done(client, review_id)
        persisted = routes.store.get(review_id)
        report = client.get(f"/api/review/{review_id}/report")
        assert report.status_code == 200
        report_text = "\n".join(p.text for p in Document(io.BytesIO(report.content)).paragraphs)
    assert review["status"] == "done"
    assert persisted["rule_pack"]["rule_pack_content_version"] == hashlib.sha256(original).hexdigest()[:12]
    expected = {"source": "local_file", "category": "technical_service",
                "content_sha256": hashlib.sha256(original).hexdigest()}
    assert persisted["rule_provenance"] == expected
    assert review["rule_provenance"] == expected
    assert expected["content_sha256"] in report_text
    assert persisted["stance_label_at_review"] == "委托方"
    assert precheck_args and precheck_args[0][6] == "委托方"
    assert next(item for item in precheck_args[0][5] if item["id"] == "technical_service")["label"] == "技术服务"
    assert "审查立场：委托方" in report_text
    assert "审查立场：新版委托方" not in report_text
    assert "新版已见主体名称" not in next(i for i in review["items"] if i["id"] == "subject")["note"]


def test_failed_review_retains_selected_snapshot_and_legacy_does_not_invent_one(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app.api import routes
    from app.main import app
    from app.services.store import ReviewStore
    from tests.helpers import wait_review_done

    isolated = ReviewStore(str(tmp_path / "isolated.sqlite"))
    monkeypatch.setattr(routes, "store", isolated)
    snapshot = capture_rule_snapshot("procurement")

    def fail(*args, **kwargs):
        raise RuntimeError("synthetic worker failure")

    monkeypatch.setattr(routes, "run_review", fail)
    with TestClient(app) as client:
        legacy = isolated.create(filename="legacy.txt", category="procurement", status="error")
        legacy_view = client.get(f"/api/review/{legacy}").json()
        assert legacy_view["rule_provenance"] is None
        assert "历史标签未留存" in legacy_view["stance_label"]
        assert "历史立场说明未留存" in legacy_view["stance_declaration"]
        uploaded = client.post("/api/upload", data={"category": "procurement", "stance": "neutral",
                                                    "pasted_text": "采购合同。甲方购买乙方商品。"})
        assert uploaded.status_code == 200
        review = wait_review_done(client, uploaded.json()["review_id"])
    assert review["status"] == "error"
    assert review["rule_provenance"]["content_sha256"] == snapshot.content_sha256


def test_saved_counterparty_notice_does_not_read_current_rules(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app.api import routes
    from app.main import app
    from app.services.store import ReviewStore

    isolated = ReviewStore(str(tmp_path / "isolated.sqlite"))
    monkeypatch.setattr(routes, "store", isolated)
    rid = isolated.create(filename="saved.txt", category="procurement", status="error",
                          precheck={"stance_notice": True}, stance_notice_at_review="历史提示")

    def unexpected(*args, **kwargs):
        raise AssertionError("must not reload current rules for saved notice")

    monkeypatch.setattr(routes.stance_service, "counterparty_view_notice", unexpected)
    with TestClient(app) as client:
        response = client.get(f"/api/review/{rid}")
    assert response.status_code == 200
    assert response.json()["precheck"]["stance_notice_text"] == "历史提示"


def test_legacy_review_survives_broken_current_stance_configuration(tmp_path, monkeypatch):
    import yaml
    from fastapi.testclient import TestClient
    from app.api import routes
    from app.main import app
    from app.services import stance
    from app.services.store import ReviewStore

    isolated = ReviewStore(str(tmp_path / "isolated.sqlite"))
    monkeypatch.setattr(routes, "store", isolated)
    rid = isolated.create(filename="legacy.txt", category="procurement", status="done",
                          precheck={"stance_notice": True}, items=[], text="合同正文")

    def broken(*args, **kwargs):
        raise yaml.YAMLError("damaged configuration")

    monkeypatch.setattr(stance, "load_checklist", broken)
    with TestClient(app) as client:
        response = client.get(f"/api/review/{rid}")
        report = client.get(f"/api/review/{rid}/report")
    assert response.status_code == 200
    assert "历史标签未留存" in response.json()["stance_label"]
    assert "当前配置不可用" in response.json()["precheck"]["stance_notice_text"]
    assert report.status_code == 200


def test_ask_http_passes_saved_label_to_model_prompt(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app.api import routes
    from app.main import app
    from app.services import llm_ask
    from app.services.store import ReviewStore

    isolated = ReviewStore(str(tmp_path / "isolated.sqlite"))
    monkeypatch.setattr(routes, "store", isolated)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "local-test-key")
    captured = {}

    def chat(_key, system, user):
        captured.update(system=system, user=user)
        return '{}'

    monkeypatch.setattr(llm_ask, "_chat_deepseek", chat)
    rid = isolated.create(filename="saved.txt", category="technical_service", status="done",
                          stance="client", stance_label_at_review="历史委托方", text="合同正文",
                          items=[{"id": "subject", "name": "主体", "status": "需关注", "note": "需核对"}])
    with TestClient(app) as client:
        response = client.post("/api/ask", json={"review_id": rid, "item_id": "subject", "question": "怎么核对？"})
    assert response.status_code == 200
    assert "历史委托方" in captured["system"] and "历史委托方" in captured["user"]


def test_upload_keeps_validated_stance_when_config_changes(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app.api import routes
    from app.main import app
    from app.services.store import ReviewStore
    from tests.helpers import wait_review_done

    for path in checklist.CONFIG_DIR.glob("checklist_*.yaml"):
        (tmp_path / path.name).write_bytes(path.read_bytes())
    target = tmp_path / "checklist_technical_service.yaml"
    monkeypatch.setattr(checklist, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(routes, "store", ReviewStore(str(tmp_path / "isolated.sqlite")))
    real_check = routes.stance_service.is_allowed
    calls = 0

    def change_after_first_check(category, stance, **kwargs):
        nonlocal calls
        result = real_check(category, stance, **kwargs)
        calls += 1
        if calls == 1:
            content = target.read_text(encoding="utf-8")
            target.write_text(content.replace("[neutral, client, vendor]", "[neutral, vendor]"), encoding="utf-8")
        return result

    monkeypatch.setattr(routes.stance_service, "is_allowed", change_after_first_check)
    with TestClient(app) as client:
        response = client.post("/api/upload", data={
            "category": "technical_service", "stance": "client",
            "pasted_text": "技术服务合同。甲方委托乙方维护系统。",
        })
        assert response.status_code == 200
        review = wait_review_done(client, response.json()["review_id"])
    assert review["status"] == "done" and review["stance"] == "client"


def test_upload_rejects_broken_snapshot_before_precheck(monkeypatch):
    import yaml
    from fastapi.testclient import TestClient
    from app.api import routes
    from app.main import app

    def broken(_category):
        raise yaml.YAMLError("synthetic")

    calls = []
    monkeypatch.setattr(routes.precheck_service, "run_precheck", lambda *args: calls.append(args))
    monkeypatch.setattr(routes, "capture_rule_snapshot", broken)
    monkeypatch.setenv("PRECHECK_ENABLED", "true")
    with TestClient(app) as client:
        response = client.post("/api/upload", data={
            "category": "technical_service", "stance": "client",
            "pasted_text": "技术服务合同。甲方委托乙方维护系统。",
        })
    assert response.status_code == 503
    assert "synthetic" not in response.text
    assert not calls


@pytest.mark.parametrize("content", ["- bad\n", "category: [bad]\n", "category: other\n"])
def test_upload_handles_invalid_category_root_without_precheck(tmp_path, monkeypatch, content):
    from fastapi.testclient import TestClient
    from app.api import routes
    from app.main import app

    for path in checklist.CONFIG_DIR.glob("checklist_*.yaml"):
        (tmp_path / path.name).write_bytes(path.read_bytes())
    (tmp_path / "checklist_broken.yaml").write_text(content, encoding="utf-8")
    monkeypatch.setattr(checklist, "CONFIG_DIR", tmp_path)
    calls = []
    monkeypatch.setattr(routes.precheck_service, "run_precheck", lambda *args: calls.append(args))
    monkeypatch.setenv("PRECHECK_ENABLED", "true")
    with TestClient(app) as client:
        response = client.post("/api/upload", data={
            "category": "technical_service", "stance": "client",
            "pasted_text": "技术服务合同。甲方委托乙方维护系统。",
        })
    assert response.status_code == 503 and not calls
