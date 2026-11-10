"""Validated immutable checklist bytes for future review-version binding.

The review pipeline uses it for rule evaluation, scoring config and the content hash.
Upload authorization, historic stance labels and deployed engine identity are separate.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any

import yaml

from app.services import checklist


def _validate_match_spec(value: Any) -> None:
    if isinstance(value, str) and value:
        try:
            re.compile(value, flags=re.IGNORECASE | re.DOTALL)
        except re.error as exc:
            raise ValueError("invalid match pattern") from exc
        return
    if isinstance(value, list) and value:
        for entry in value:
            _validate_match_spec(entry)
        return
    if isinstance(value, dict) and len(value) == 1:
        key = next(iter(value))
        if key in {"pattern", "any_of", "all_of"}:
            _validate_match_spec(value[key])
            return
    raise ValueError("invalid match specification")


def _validate_rule_shape(rule: dict[str, Any]) -> None:
    if not any(key in rule for key in ("matcher", "pattern", "any_of", "all_of", "hit_alternatives")):
        raise ValueError("missing rule matcher")
    if "matcher" in rule and (not isinstance(rule["matcher"], str) or not rule["matcher"].strip()):
        raise ValueError("invalid matcher name")
    if "pattern" in rule and not isinstance(rule["pattern"], str):
        raise ValueError("rule pattern must be a string")
    for field in ("pattern", "any_of", "all_of", "unless", "none_of"):
        if field in rule:
            _validate_match_spec(rule[field])
    if "unless_window" in rule and (not isinstance(rule["unless_window"], int)
                                    or isinstance(rule["unless_window"], bool)
                                    or rule["unless_window"] < 0):
        raise ValueError("invalid rule window")
    if "hit_alternatives" in rule:
        alternatives = rule["hit_alternatives"]
        if not isinstance(alternatives, list) or not alternatives:
            raise ValueError("invalid hit alternatives")
        for entry in alternatives:
            if (not isinstance(entry, dict) or not isinstance(entry.get("pattern"), str)
                    or not isinstance(entry.get("start_token"), str)
                    or not entry["pattern"] or not entry["start_token"]
                    or not isinstance(entry.get("neg_sensitive"), bool)):
                raise ValueError("invalid hit alternative")
            try:
                if re.compile(entry["pattern"]).groups:
                    raise ValueError("capturing groups are not supported in hit alternatives")
            except re.error as exc:
                raise ValueError("invalid hit alternative pattern") from exc
        try:
            checklist._first_unprotected_hit_alt("", rule)
        except (re.error, ValueError, TypeError, IndexError) as exc:
            raise ValueError("invalid hit alternative pattern") from exc


@dataclass(frozen=True)
class RuleSnapshot:
    """Immutable original bytes and their content identifier, not an engine version."""

    category: str
    config_bytes: bytes
    content_version: str

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-z0-9_]+", self.category):
            raise ValueError("invalid category")
        if hashlib.sha256(self.config_bytes).hexdigest()[:12] != self.content_version:
            raise ValueError("snapshot content version does not match bytes")
        config = yaml.safe_load(self.config_bytes)
        if not isinstance(config, dict) or config.get("category") != self.category:
            raise ValueError(f"invalid category configuration: {self.category}")
        scorecard = config.get("scorecard")
        if scorecard is not None and (not isinstance(scorecard, dict)
                                      or not isinstance(scorecard.get("segments", []), list)
                                      or not all(isinstance(s, dict) for s in scorecard.get("segments", []))):
            raise ValueError(f"invalid scorecard configuration: {self.category}")
        if isinstance(scorecard, dict):
            for segment in scorecard.get("segments", []):
                weight = segment.get("weight", 0)
                if isinstance(weight, bool) or not isinstance(weight, int) or not 0 <= weight <= 100:
                    raise ValueError(f"invalid scorecard weight: {self.category}")
        stances = config.get("stances")
        if stances is not None and (not isinstance(stances, dict)
                                    or not isinstance(stances.get("allowed", []), list)
                                    or not isinstance(stances.get("labels", {}), dict)
                                    or (stances.get("view") is not None and not isinstance(stances.get("view"), str))):
            raise ValueError(f"invalid stance configuration: {self.category}")
        items = config.get("items")
        if not isinstance(items, list) or not items:
            raise ValueError(f"empty or missing checklist items: {self.category}")
        for item in items:
            if (not isinstance(item, dict) or not item.get("id") or not item.get("name")
                    or (not item.get("na") and not isinstance(item.get("rules"), dict))):
                raise ValueError(f"incomplete checklist item: {self.category}")
            if "rules" in item and not isinstance(item["rules"], dict):
                raise ValueError(f"invalid checklist rules: {self.category}")
            if not item.get("na") and not any(
                item["rules"].get(phase) for phase in ("pass", "need_attention")
            ):
                raise ValueError(f"checklist item has no matching rules: {self.category}")
            for phase in ("pass", "need_attention"):
                rules = (item.get("rules") or {}).get(phase, [])
                if not isinstance(rules, list) or not all(isinstance(rule, dict) for rule in rules):
                    raise ValueError(f"invalid checklist rules: {self.category}")
                for rule in rules:
                    _validate_rule_shape(rule)
        checklist._validate_category_config(self.category, config)
        checklist._validate_matchers(checklist.CONFIG_DIR / f"checklist_{self.category}.yaml", config)

    def configuration(self) -> dict[str, Any]:
        # Reparse immutable bytes so callers cannot mutate another caller's copy.
        return yaml.safe_load(self.config_bytes)

    @property
    def content_sha256(self) -> str:
        """Full identifier of these bytes; does not identify the running engine."""
        return hashlib.sha256(self.config_bytes).hexdigest()


def capture_rule_snapshot(category: str) -> RuleSnapshot:
    """Read once, validate the same bytes, then retain those exact bytes and hash."""
    if not re.fullmatch(r"[a-z0-9_]+", category):
        raise ValueError("invalid category")
    path = checklist.CONFIG_DIR / f"checklist_{category}.yaml"
    raw = path.read_bytes()
    return RuleSnapshot(
        category=category,
        config_bytes=raw,
        content_version=hashlib.sha256(raw).hexdigest()[:12],
    )
