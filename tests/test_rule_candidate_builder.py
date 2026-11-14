"""A proposal is converted into a validated immutable candidate, not published."""
import hashlib

import pytest

from app.services.rule_candidate_builder import build_candidate
from app.services.rule_snapshot import capture_rule_snapshot


@pytest.fixture
def snapshot():
    return capture_rule_snapshot("procurement")


def proposal(snapshot, **overrides):
    return {
        "category": "procurement", "operation": "add", "item_id": "custom_delivery",
        "name": "交付清单", "description": "检查是否约定交付清单", "reason": "明确交付内容",
        "segment": "B", "literal": "交付清单（附件）", "positive": "按交付清单（附件）验收",
        "negative": "按验收单办理", "base_sha256": hashlib.sha256(snapshot.config_bytes).hexdigest(),
        **overrides,
    }


def test_add_constructs_escaped_literal_and_executes_examples(snapshot):
    result = build_candidate(snapshot, proposal(snapshot))
    candidate = result.snapshot.configuration()
    assert candidate["items"][-1]["id"] == "custom_delivery"
    assert result.reason == "明确交付内容"
    assert result.example_results == {"positive": "通过", "negative": "未找到"}
    assert result.change["before"] is None
    assert result.change["after"]["literal"] == "交付清单（附件）"
    assert hashlib.sha256(result.snapshot.config_bytes).hexdigest() != result.base_sha256
    assert "custom_delivery" not in {i["id"] for i in snapshot.configuration()["items"]}


def test_invalid_examples_or_stale_baseline_do_not_produce_candidate(snapshot):
    with pytest.raises(ValueError, match="正反例"):
        build_candidate(snapshot, proposal(snapshot, negative="交付清单（附件）"))
    with pytest.raises(ValueError, match="基线"):
        build_candidate(snapshot, proposal(snapshot, base_sha256="0" * 64))


def test_disable_enable_and_hardline_protection(snapshot):
    result = build_candidate(snapshot, proposal(snapshot, operation="disable", item_id="subject_matter"))
    item = next(i for i in result.snapshot.configuration()["items"] if i["id"] == "subject_matter")
    assert item["enabled"] is False
    assert result.change["before"]["enabled"] is True and result.change["after"]["enabled"] is False
    enabled = build_candidate(result.snapshot, proposal(result.snapshot, operation="enable", item_id="subject_matter"))
    assert next(i for i in enabled.snapshot.configuration()["items"] if i["id"] == "subject_matter")["enabled"] is True
    hardline = next(i for i in snapshot.configuration()["items"] if "hardline" in str(i))
    with pytest.raises(ValueError, match="硬性"):
        build_candidate(snapshot, proposal(snapshot, operation="disable", item_id=hardline["id"]))


def test_literal_preserves_significant_spaces_and_noop_edit_is_rejected(snapshot):
    result = build_candidate(snapshot, proposal(snapshot, literal=" 交付清单 ", positive="前 交付清单 后", negative="交付清单"))
    assert result.example_results == {"positive": "通过", "negative": "未找到"}
    item = next(i for i in snapshot.configuration()["items"] if i["id"] == "subject_matter")
    with pytest.raises(ValueError, match="没有变化"):
        build_candidate(snapshot, proposal(snapshot, operation="edit", item_id=item["id"], name=item["name"], description=item["description"]))


def test_duplicate_display_name_or_exact_matching_pattern_is_rejected(snapshot):
    with pytest.raises(ValueError, match="名称.*重复"):
        build_candidate(snapshot, proposal(snapshot, name=" 主 体 "))
    with pytest.raises(ValueError, match="关键词.*重复"):
        build_candidate(snapshot, proposal(snapshot, literal="法定代表人", positive="法定代表人张三", negative="统一社会信用代码"))
    with pytest.raises(ValueError, match="不可见"):
        build_candidate(snapshot, proposal(snapshot, name="主\u200b体"))


def test_literal_conflicts_follow_python_regex_case_semantics():
    from app.services.rule_candidate_builder import validate_candidate_conflicts

    base = {"items": [{"id": "old", "name": "旧项", "rules": {"pass": [{"pattern": "ß"}]}}]}
    candidate = {"items": [*base["items"], {"id": "new", "name": "新项", "rules": {"pass": [{"pattern": "ss"}]}}]}
    validate_candidate_conflicts(base, candidate)  # re.IGNORECASE does not equate ß and ss.
    base["items"][0]["rules"]["pass"][0]["pattern"] = "i"
    candidate["items"][-1]["rules"]["pass"][0]["pattern"] = "ı"
    with pytest.raises(ValueError, match="关键词.*重复"):
        validate_candidate_conflicts(base, candidate)
