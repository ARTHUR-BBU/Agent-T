"""Build a constrained rule candidate from a fixed server-supplied snapshot.

This does not store, approve, publish, or certify a candidate. In particular,
the two example outcomes are not a substitute for release regression evidence.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Mapping

import yaml

from app.services import checklist
from app.services.rule_snapshot import RuleSnapshot


@dataclass(frozen=True)
class CandidateBuild:
    snapshot: RuleSnapshot
    base_sha256: str
    reason: str
    example_results: dict[str, str]
    change: dict[str, Any]


def _text(draft: Mapping[str, Any], key: str, maximum: int) -> str:
    value = draft.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{key} 必须为有效文本")
    return value


def _summary(item: dict[str, Any]) -> dict[str, Any]:
    return {"id": item["id"], "name": item["name"], "description": item.get("description", ""),
            "segment": item.get("segment", ""), "enabled": item.get("enabled", True)}


def _patterns(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for child in value:
            yield from _patterns(child)
    elif isinstance(value, dict):
        for key in ("pattern", "any_of", "all_of"):
            if key in value:
                yield from _patterns(value[key])


def validate_candidate_conflicts(baseline: dict[str, Any], candidate: dict[str, Any]) -> None:
    """Catch newly introduced exact-name and literal-pattern duplicates."""
    def groups(items):
        result: dict[str, set[str]] = {}
        for item in items:
            name = "".join(unicodedata.normalize("NFKC", str(item.get("name") or "")).split()).casefold()
            result.setdefault(name, set()).add(str(item["id"]))
        return result

    old_items = {str(item["id"]): item for item in baseline["items"]}
    for item in candidate["items"]:
        old_item = old_items.get(str(item["id"]))
        name = item.get("name")
        if (old_item is None or name != old_item.get("name")) and isinstance(name, str):
            if any(unicodedata.category(char) in {"Cc", "Cf", "Cs"} for char in name):
                raise ValueError("检查项名称不能包含不可见控制字符")
    old, new = groups(baseline["items"]), groups(candidate["items"])
    if any(len(ids) > 1 and not ids.issubset(old.get(name, set())) for name, ids in new.items()):
        raise ValueError("检查项名称与已有项目重复")
    patterns: set[str] = set()
    for item in baseline["items"]:
        for phase in ("pass", "need_attention"):
            for rule in (item.get("rules") or {}).get(phase, []):
                for pattern in _patterns(rule):
                    literal = re.sub(r"\\(.)", r"\1", pattern, flags=re.DOTALL)
                    if re.escape(literal) == pattern:
                        patterns.add(literal)
    for item in candidate["items"][len(baseline["items"]):]:
        pattern = item["rules"]["pass"][0]["pattern"]
        literal = re.sub(r"\\(.)", r"\1", pattern, flags=re.DOTALL)
        if any(re.fullmatch(re.escape(existing), literal, flags=re.IGNORECASE | re.DOTALL)
               and re.fullmatch(re.escape(literal), existing, flags=re.IGNORECASE | re.DOTALL)
               for existing in patterns):
            raise ValueError("新增关键词与已有规则重复")
        patterns.add(literal)


def build_candidate(base: RuleSnapshot, draft: Mapping[str, Any]) -> CandidateBuild:
    """Convert one proposal using validated literal rules and original item order."""
    base_digest = hashlib.sha256(base.config_bytes).hexdigest()
    if draft.get("category") != base.category or draft.get("base_sha256") != base_digest:
        raise ValueError("规则基线已变化")
    reason = _text(draft, "reason", 500).strip()
    item_id = _text(draft, "item_id", 64)
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", item_id):
        raise ValueError("检查项编号无效")
    operation = draft.get("operation")
    if not isinstance(operation, str) or operation not in {"add", "edit", "disable", "enable"}:
        raise ValueError("未知变更方式")
    config = base.configuration()
    item = next((i for i in config["items"] if i["id"] == item_id), None)
    before = _summary(item) if item is not None else None
    examples: dict[str, str] = {}
    if operation == "add":
        if item is not None:
            raise ValueError("检查项编号已存在")
        name = _text(draft, "name", 120)
        description = _text(draft, "description", 1000)
        segment = _text(draft, "segment", 20)
        literal = _text(draft, "literal", 120)
        positive = _text(draft, "positive", 2000)
        negative = _text(draft, "negative", 2000)
        item = {"id": item_id, "name": name, "description": description, "class": "existence",
                "segment": segment, "rules": {"pass": [{"pattern": re.escape(literal)}]}}
        isolated = {"category": base.category, "items": [item]}
        examples = {"positive": checklist.run_checklist(positive, base.category, configuration=isolated)["items"][0]["status"],
                    "negative": checklist.run_checklist(negative, base.category, configuration=isolated)["items"][0]["status"]}
        if examples != {"positive": checklist.STATUS_PASS, "negative": checklist.STATUS_NOT_FOUND}:
            raise ValueError("候选规则未通过正反例测试")
        config["items"].append(item)
    else:
        if item is None:
            raise ValueError("检查项不存在")
        if checklist._contains_hardline(item):
            raise ValueError("普通规则变更不能修改硬性规则")
        if operation == "edit":
            name = _text(draft, "name", 120)
            description = _text(draft, "description", 1000)
            if item.get("name") == name and item.get("description", "") == description:
                raise ValueError("检查项没有变化")
            item["name"] = name
            item["description"] = description
        else:
            current = item.get("enabled", True)
            desired = operation == "enable"
            if current is desired:
                raise ValueError("检查项已经处于目标状态")
            item["enabled"] = desired
    raw = yaml.safe_dump(config, allow_unicode=True, sort_keys=False).encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()
    snapshot = RuleSnapshot(category=base.category, config_bytes=raw, content_version=digest[:12])
    validate_candidate_conflicts(base.configuration(), snapshot.configuration())
    after = _summary(item)
    if operation == "add":
        after["literal"] = literal
    return CandidateBuild(snapshot=snapshot, base_sha256=base_digest, reason=reason,
                          example_results=examples, change={"operation": operation, "before": before, "after": after})
