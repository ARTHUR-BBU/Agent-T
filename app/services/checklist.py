"""Rules-first checklist engine driven by YAML configs."""
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
        "note": "",
        "quote": "",
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
        if _match(text, rule.get("pattern", "")):
            unless = rule.get("unless")
            if unless and _match(text, unless):
                continue
            quote = _extract_quote(text, rule.get("pattern", ""))
            return {
                **base,
                "status": STATUS_ATTENTION,
                "note": rule.get("note", "需关注"),
                "quote": quote,
            }

    # 2) pass
    for rule in rules.get("pass") or []:
        if _match(text, rule.get("pattern", "")):
            quote = _extract_quote(text, rule.get("pattern", ""))
            return {
                **base,
                "status": STATUS_PASS,
                "note": rule.get("note", "条款基本可接受"),
                "quote": quote,
            }

    # 3) not found / missing
    missing_as = item.get("missing_as", STATUS_NOT_FOUND)
    missing_note = item.get("missing_note", "未在合同中找到相关约定")
    # If not_found patterns exist, we still treat overall miss as missing
    return {
        **base,
        "status": missing_as,
        "note": missing_note if missing_as == STATUS_ATTENTION else "未在合同中找到相关约定",
        "quote": "",
    }


def _match(text: str, pattern: str) -> bool:
    if not pattern:
        return False
    try:
        return re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL) is not None
    except re.error:
        return pattern in text


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
