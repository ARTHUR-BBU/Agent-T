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
    cfg = _load_yaml(path)
    _validate_segment_mapping(cfg, category)
    return cfg


def _validate_segment_mapping(cfg: dict[str, Any], category: str) -> None:
    """配置校验（遗留项⑤ + 老钱 L-1 加严 + 小智娘 P2-2）：声明了 scorecard 块时——

    1. 分段 key 不得重复（重复分段 postprocess 会把 total 相加出 >100 的荒谬分）；
    2. 每个 item 必须有 segment 且指向存在的分段 key——缺失、None、0、"" 等
       falsy 值同责 fail-fast（静默退出评分会让扣分下限/封顶/补点名全部漏掉该项）。

    没有 scorecard 块的旧品类配置跳过校验（评分未开通是合法态）。
    """
    segments = (cfg.get("scorecard") or {}).get("segments") or []
    if not segments:
        return
    keys = [str(s.get("key")) for s in segments if s.get("key")]
    dupes = sorted({k for k in keys if keys.count(k) > 1})
    if dupes:
        raise ValueError(f"品类 {category} 配置错误：评分卡分段 key 重复 {dupes}（重复分段会导致总分异常）")
    valid = sorted(set(keys))
    bad = sorted(
        {
            repr(i.get("segment"))
            for i in cfg.get("items", [])
            # 缺失/None/falsy（0、false、""）同责：静默退出评分是漏封顶通道（老钱 L-1）；
            # 数字 key 保留 str 强转语义（YAML 两侧一致归一，小智娘测试钉死）
            if i.get("segment") is None or str(i.get("segment")) not in valid
        }
    )
    if bad:
        raise ValueError(
            f"品类 {category} 配置错误：item.segment 缺失或引用了不存在的评分卡分段 {bad}（可用分段：{valid}）"
        )


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
    # 完备型检查项开关（小智娘租赁终验 P2，2026-09-06）：subject 类 unless 是
    # 「命中词附近有没有补全信息」的邻近判定，补全信息（法定代表人/信用代码）
    # 常落在签署页（>60 字外）。开启后，unless 窗口未放行时先查 pass 词表全文——
    # 命中即通过，不再按「未见补全」定需关注（避免 note 与事实相反）。
    pass_fulltext_fallback = bool(item.get("pass_fulltext_fallback"))

    # 1) need_attention first (highest priority)
    for rule in rules.get("need_attention") or []:
        if _rule_matches(text, rule) and _unless_holds(text, rule):
            if pass_fulltext_fallback and _pass_matches(rules, text):
                break  # 补全信息在全文存在：完备型误报，交由 pass 定「通过」
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
        if _rule_matches(text, rule) and _unless_holds(text, rule):
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
    # 语义（老钱意见书 L-2，2026-09-06）：rules.not_found 块是文档性配置，不参与判定——
    # 「未找到」的真实触发条件是 need_attention 与 pass 词表均未命中。
    # 因此 pass 词表必须足够宽（覆盖合同高频必备词），否则正常合同会被误判
    # 「未找到」并触发 89 封顶；missing_as 决定缺项档位（默认「未找到」，可设「需关注」加严）。
    missing_as = item.get("missing_as", STATUS_NOT_FOUND)
    missing_note = item.get("missing_note", "未在合同中找到相关约定")
    return {
        **base,
        "status": missing_as,
        "note": missing_note if missing_as == STATUS_ATTENTION else "未在合同中找到相关约定",
        "quote": "",
        "hits": [],
    }


_UNLESS_WINDOW = 60


def _pass_matches(rules: dict[str, Any], text: str) -> bool:
    """pass 词表全文域匹配（供 pass_fulltext_fallback 完备型兜底用）。"""
    for rule in rules.get("pass") or []:
        if _rule_matches(text, rule):
            return True
    return False


def _unless_holds(text: str, rule: dict[str, Any]) -> bool:
    """unless/none_of 只在与正向命中邻近的局部窗口（±60 字符）内生效。

    旧实现是全文域：合同任意位置的保护性表述会放空风险句——租赁终验实测
    「甲方未取得产权人书面同意对外转租」被全文另一处的「乙方不得擅自转租」
    boilerplate 洗成通过（小智娘 2026-09-06 P1 根因）。规则没有 unless/none_of
    时行为完全不变。
    """
    if rule.get("unless") is None and rule.get("none_of") is None:
        return True
    return not _negative_evidence(_match_neighborhood(text, rule), rule)


def _match_neighborhood(text: str, rule: dict[str, Any]) -> str:
    """第一个正向命中的邻近窗口。

    注意（小智娘终验 P3②，2026-09-06）：_patterns_from_spec 会把 all_of 拆平，
    本函数锚定的是第一个可匹配 conjunct 的位置——all_of 联合命中区间可能更宽，
    「定位不到即回退全文」的安全网实际不存在（当前三品类无 all_of+unless 组合，
    零影响）。若未来引入该组合，需改为各 conjunct 命中区间的最小共同邻域。
    """
    top = {k: rule[k] for k in ("pattern", "any_of", "all_of") if k in rule}
    for pattern in _patterns_from_spec(top):
        try:
            m = re.search(pattern, text)
        except re.error:
            continue
        if m:
            lo = max(0, m.start() - _UNLESS_WINDOW)
            hi = min(len(text), m.end() + _UNLESS_WINDOW)
            return text[lo:hi]
    return text


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
