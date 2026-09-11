"""Rules-first checklist engine driven by YAML configs.

Supports structured rule matching (any_of / all_of / none_of / pattern)
so synonym and paraphrase groups stay deterministic without an LLM.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Optional

import yaml

logger = logging.getLogger(__name__)

CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"

STATUS_PASS = "通过"
STATUS_ATTENTION = "需关注"
STATUS_NOT_FOUND = "未找到"
STATUS_NA = "本类不适用"


def list_categories() -> list[dict[str, Any]]:
    cats = []
    for path in sorted(CONFIG_DIR.glob("checklist_*.yaml")):
        data = _load_yaml(path)
        cats.append(
            {
                "id": data.get("category", path.stem.replace("checklist_", "")),
                "label": data.get("label", path.stem),
                # 阶段 1.3：立场元数据透传（前端 segmented 数据源；缺失时
                # 上层 stance.get_stances 会回默认，这里原样透出）
                "stances": data.get("stances") or {},
            }
        )
    return cats


def load_checklist(category: str) -> dict[str, Any]:
    path = CONFIG_DIR / f"checklist_{category}.yaml"
    if not path.exists():
        # fallback to procurement（老钱预审金标附警告：静默兜底是「服务合同按
        # 采购硬审」事故的结构性温床——保留兼容但必须留痕，上游 precheck
        # 生效路径上未知品类已显式走 category_confirm，不再落到这里）
        logger.warning("checklist fallback: unknown category=%s -> procurement", category)
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


def validate_checklist_configs() -> None:
    """启动/CI 校验（外部审计批2-④）：全部品类的规则正则必须可编译、
    item id 不得重复——配置坏了宁可起不来，也不要悄悄降级漏审
    （_match 的 re.error→子串匹配回退是运行期容错，不是配置错误的豁免）。"""
    problems: list[str] = []
    for cat_info in list_categories():
        category = cat_info["id"]
        cfg = _load_yaml(CONFIG_DIR / f"checklist_{category}.yaml")
        seen_ids: set[str] = set()
        for item in cfg.get("items", []):
            iid = str(item.get("id") or "")
            if not iid:
                problems.append(f"{category}: 存在无 id 的 item")
                continue
            if iid in seen_ids:
                problems.append(f"{category}: item id 重复 {iid}")
            seen_ids.add(iid)
            for rule in (item.get("rules") or {}).get("need_attention") or []:
                problems.extend(_regex_problems(category, iid, rule))
            for rule in (item.get("rules") or {}).get("pass") or []:
                problems.extend(_regex_problems(category, iid, rule))
    if problems:
        raise ValueError(
            "checklist 配置校验失败（fail-closed）：\n- " + "\n- ".join(problems)
        )


def _regex_problems(category: str, item_id: str, rule: dict[str, Any]) -> list[str]:
    """收集单条规则里所有正则的编译错误（含 unless/none_of 子规则）。"""
    out: list[str] = []
    specs: list[tuple[str, Any]] = [
        ("pattern", rule.get("pattern")),
        ("any_of", rule.get("any_of")),
        ("all_of", rule.get("all_of")),
        ("unless", rule.get("unless")),
        ("none_of", rule.get("none_of")),
    ]
    for field, spec in specs:
        for pat in _patterns_from_spec(spec):
            try:
                re.compile(pat, flags=re.IGNORECASE | re.DOTALL)
            except re.error as exc:
                out.append(f"{category}/{item_id} rules.{field} 非法正则 {pat!r}: {exc}")
    return out


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
    # MatchEvidence（外部审计二轮 P1-1）：一次扫描同时定档位与证据坐标，
    # quote/条款归属均从同一 occurrence 派生——旧实现判定与摘句各自重扫，
    # 「风险在第二处、摘句引第一处」的结论-证据错位即源于此
    for rule in rules.get("need_attention") or []:
        evidence = _first_unprotected_occurrence(text, rule)
        if evidence:
            if pass_fulltext_fallback and _pass_matches(rules, text):
                break  # 补全信息在全文存在：完备型误报，交由 pass 定「通过」
            return {
                **base,
                "status": STATUS_ATTENTION,
                "note": rule.get("note", "需关注"),
                "quote": _quote_from_evidence(text, evidence),
                "hits": _extract_hits_from_rule(text, rule),
                "evidence_start": evidence["start"],
                "evidence_end": evidence["end"],
            }

    # 2) pass
    for rule in rules.get("pass") or []:
        evidence = _first_unprotected_occurrence(text, rule)
        if evidence:
            return {
                **base,
                "status": STATUS_PASS,
                "note": rule.get("note", "条款基本可接受"),
                "quote": _quote_from_evidence(text, evidence),
                "hits": _extract_hits_from_rule(text, rule),
                "evidence_start": evidence["start"],
                "evidence_end": evidence["end"],
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


def _first_unprotected_occurrence(text: str, rule: dict[str, Any]) -> Optional[dict[str, Any]]:
    """一次命中只产出一份证据（MatchEvidence，外部审计二轮 P1-1）。

    返回首个「邻近无保护」的风险 occurrence：{"start","end","text","pattern"}；
    规则不成立（未命中 / all_of 缺 conjunct / 全部命中受保护 / 非法正则）返回
    None。status、quote、primary_clause 全部从这一份坐标派生——旧行为是
    判定、摘句、命中各自重扫，「结论正确、证据错误」（风险在第二处、摘句
    引第一处）的结构性根因即此。

    unless/none_of 只与命中邻近的局部窗口（±60 字符）内生效（承批2 逐
    occurrence 判定）：无 unless/none_of 的规则任何命中即证据（首个命中，
    与历史行为一致）。all_of 先做全 conjunct 命中前置校验（对齐原
    _rule_matches 的 AND 语义），证据取首个无保护的 conjunct occurrence。
    非法正则视为无命中留痕（外部审计批3：启动校验已保证运行期不可达）。
    """
    top = {k: rule[k] for k in ("pattern", "any_of", "all_of") if k in rule}
    if "all_of" in rule:
        pats = _patterns_from_spec(top)
        if not pats or not all(_match(text, p) for p in pats):
            return None
    for pattern in _patterns_from_spec(top):
        try:
            matches = re.finditer(pattern, text, flags=re.IGNORECASE | re.DOTALL)
        except re.error:
            logging.getLogger(__name__).warning(
                "Invalid regex treated as no-match in evidence scan: %r", pattern
            )
            continue
        for m in matches:
            lo = max(0, m.start() - _UNLESS_WINDOW)
            hi = min(len(text), m.end() + _UNLESS_WINDOW)
            if not _negative_evidence(text[lo:hi], rule):
                return {
                    "start": m.start(),
                    "end": m.end(),
                    "text": m.group(0),
                    "pattern": pattern,
                }
    return None


def _quote_from_evidence(text: str, evidence: dict[str, Any], window: int = 40) -> str:
    """摘句从证据坐标切窗（替代旧 re.search 重扫首处）。"""
    start = max(0, evidence["start"] - window)
    end = min(len(text), evidence["end"] + window)
    snippet = text[start:end].replace("\n", " ").strip()
    if start > 0:
        snippet = "…" + snippet
    if end < len(text):
        snippet = snippet + "…"
    return snippet


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



