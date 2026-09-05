"""Rules-first checklist engine driven by YAML configs.

Supports structured rule matching (any_of / all_of / none_of / pattern)
so synonym and paraphrase groups stay deterministic without an LLM.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"

STATUS_PASS = "通过"
STATUS_ATTENTION = "需关注"
STATUS_NOT_FOUND = "未找到"
STATUS_NA = "本类不适用"


def list_categories() -> list[dict[str, str]]:
    cats = []
    for path in sorted(CONFIG_DIR.glob("checklist_*.yaml")):
        data = _load_yaml(path)
        cats.append(
            {
                "id": data.get("category", path.stem.replace("checklist_", "")),
                "label": data.get("label", path.stem),
            }
        )
    return cats


def load_checklist(category: str) -> dict[str, Any]:
    path = CONFIG_DIR / f"checklist_{category}.yaml"
    if not path.exists():
        # default to procurement
        path = CONFIG_DIR / "checklist_procurement.yaml"
        if not path.exists():
            raise FileNotFoundError(f"No checklist config for category={category}")
    return _load_yaml(path)


def run_checklist(text: str, category: str = "procurement") -> dict[str, Any]:
    cfg = load_checklist(category)
    items_out: list[dict[str, Any]] = []
    for item in cfg.get("items", []):
        items_out.append(_eval_item(text, item))
    return {
        "category": cfg.get("category", category),
        "category_label": cfg.get("label", category),
        "policies": cfg.get("policies", []),
        "items": items_out,
    }


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _eval_item(text: str, item: dict[str, Any]) -> dict[str, Any]:
    base = {
        "id": item["id"],
        "name": item["name"],
        # M3.5 评分卡分段（scorecard.py 按 segment 归项扣分/封顶）
        "segment": item.get("segment", ""),
        "note": "",
        "quote": "",
        "hits": [],
        "category_na": False,
    }

    if item.get("na"):
        return {
            **base,
            "status": STATUS_NA,
            "note": item.get("na_reason", "本类不适用"),
            "category_na": True,
        }

    rules = item.get("rules") or {}

    # 1) need_attention first (highest priority)
    for rule in rules.get("need_attention") or []:
        if _rule_matches(text, rule) and not _negative_evidence(text, rule):
            quote = _extract_quote_from_rule(text, rule)
            hits = _extract_hits_from_rule(text, rule)
            return {
                **base,
                "status": STATUS_ATTENTION,
                "note": rule.get("note", "需关注"),
                "quote": quote,
                "hits": hits,
            }

    # 2) pass
    for rule in rules.get("pass") or []:
        if _rule_matches(text, rule) and not _negative_evidence(text, rule):
            quote = _extract_quote_from_rule(text, rule)
            hits = _extract_hits_from_rule(text, rule)
            return {
                **base,
                "status": STATUS_PASS,
                "note": rule.get("note", "条款基本可接受"),
                "quote": quote,
                "hits": hits,
            }

    # 3) not found / missing
    missing_as = item.get("missing_as", STATUS_NOT_FOUND)
    missing_note = item.get("missing_note", "未在合同中找到相关约定")
    return {
        **base,
        "status": missing_as,
        "note": missing_note if missing_as == STATUS_ATTENTION else "未在合同中找到相关约定",
        "quote": "",
        "hits": [],
    }


def _patterns_from_spec(spec: Any) -> list[str]:
    """Normalize a match spec into a list of regex/literal patterns.

    Accepted forms:
      - "pattern"
      - ["p1", "p2"]
      - {"pattern": "..."}
      - {"any_of": [...]}
      - {"all_of": [...]}  (returned as-is for caller; see _rule_matches)
    """
    if spec is None or spec == "":
        return []
    if isinstance(spec, str):
        return [spec]
    if isinstance(spec, list):
        out: list[str] = []
        for item in spec:
            out.extend(_patterns_from_spec(item))
        return out
    if isinstance(spec, dict):
        if "any_of" in spec:
            return _patterns_from_spec(spec["any_of"])
        if "pattern" in spec:
            return _patterns_from_spec(spec["pattern"])
        if "all_of" in spec:
            return _patterns_from_spec(spec["all_of"])
    return []


def _rule_matches(text: str, rule: dict[str, Any]) -> bool:
    """Positive match: pattern / any_of (OR) / all_of (AND)."""
    if "all_of" in rule:
        pats = _patterns_from_spec(rule["all_of"])
        return bool(pats) and all(_match(text, p) for p in pats)
    if "any_of" in rule:
        pats = _patterns_from_spec(rule["any_of"])
        return any(_match(text, p) for p in pats)
    if "pattern" in rule:
        return _match(text, rule.get("pattern", ""))
    # bare string list not expected at top level
    return False


def _negative_evidence(text: str, rule: dict[str, Any]) -> bool:
    """True if negative evidence blocks the rule (unless / none_of).

    - unless: string | list | {any_of|pattern|all_of} — if it matches, block
    - none_of: list of patterns — if ANY matches, block (same as unless any_of)
    """
    unless = rule.get("unless")
    if unless is not None:
        if isinstance(unless, dict):
            if _rule_matches(text, unless):
                return True
        else:
            for p in _patterns_from_spec(unless):
                if _match(text, p):
                    return True

    none_of = rule.get("none_of")
    if none_of is not None:
        for p in _patterns_from_spec(none_of):
            if _match(text, p):
                return True
    return False


def _match(text: str, pattern: str) -> bool:
    if not pattern:
        return False
    try:
        return re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL) is not None
    except re.error:
        return pattern in text



def _extract_hits_from_rule(text: str, rule: dict[str, Any]) -> list[str]:
    """Return matched substrings from the contract for positive rule patterns."""
    candidates: list[str] = []
    if "all_of" in rule:
        candidates = _patterns_from_spec(rule["all_of"])
    elif "any_of" in rule:
        candidates = _patterns_from_spec(rule["any_of"])
    elif "pattern" in rule:
        candidates = [rule.get("pattern", "")]
    hits: list[str] = []
    seen: set[str] = set()
    for pat in candidates:
        if not pat:
            continue
        try:
            for m in re.finditer(pat, text, flags=re.IGNORECASE | re.DOTALL):
                frag = (m.group(0) or "").strip()
                if not frag or frag in seen:
                    continue
                if len(frag) > 40:
                    frag = frag[:40].strip()
                seen.add(frag)
                hits.append(frag)
        except re.error:
            if pat in text and pat not in seen:
                seen.add(pat)
                hits.append(pat)
    hits.sort(key=len, reverse=True)
    return hits


def _extract_quote_from_rule(text: str, rule: dict[str, Any], window: int = 40) -> str:
    """Quote around the first hitting positive pattern in the rule."""
    candidates: list[str] = []
    if "all_of" in rule:
        candidates = _patterns_from_spec(rule["all_of"])
    elif "any_of" in rule:
        candidates = _patterns_from_spec(rule["any_of"])
    elif "pattern" in rule:
        candidates = [rule.get("pattern", "")]
    for pat in candidates:
        quote = _extract_quote(text, pat, window=window)
        if quote:
            return quote
    return ""


def _extract_quote(text: str, pattern: str, window: int = 40) -> str:
    if not pattern:
        return ""
    try:
        m = re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL)
    except re.error:
        m = None
        idx = text.find(pattern)
        if idx >= 0:
            start = max(0, idx - window)
            end = min(len(text), idx + len(pattern) + window)
            return text[start:end].strip()
        return ""
    if not m:
        return ""
    start = max(0, m.start() - window)
    end = min(len(text), m.end() + window)
    snippet = text[start:end].replace("\n", " ").strip()
    if start > 0:
        snippet = "…" + snippet
    if end < len(text):
        snippet = snippet + "…"
    return snippet
