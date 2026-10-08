"""Rules-first checklist engine driven by YAML configs.

Supports structured rule matching (any_of / all_of / none_of / pattern)
so synonym and paraphrase groups stay deterministic without an LLM.
F-1/F-2 第一批（设计稿 v1.9.2，2026-10-07）：新增预注册匹配器注册表
（YAML 只能引用注册名，禁止动态导入）与 hit_alternatives 逐 occurrence
判定结构（否定上下文 + 对等豁免链 + occurrence 锚定 + 窗口化搜索）。
"""
from __future__ import annotations

import functools
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

import yaml

logger = logging.getLogger(__name__)

CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"

STATUS_PASS = "通过"
STATUS_ATTENTION = "需关注"
STATUS_NOT_FOUND = "未找到"
STATUS_NA = "本类不适用"


# ============================================================
# 预注册匹配器注册表（设计稿 v1.9.2 §三④b）
# YAML 只能引用此注册表中预注册的名字（`matcher: procurement_term`），
# 禁止从配置动态导入模块/函数路径；未知名字在 load_checklist 阶段
# fail-fast 报错（错误信息含配置文件/检查项/matcher 名）。
# ============================================================


@dataclass(frozen=True)
class MatchResult:
    """匹配器命中对象（五字段，一次匹配多处复用：状态/quote/hits/全文兜底）。"""

    matched: bool
    start: int | None
    end: int | None
    evidence: str
    hit_type: str


_MATCHERS: dict[str, Callable[[str], MatchResult]] = {}


def _register_matcher(name: str) -> Callable[[Callable[[str], MatchResult]], Callable[[str], MatchResult]]:
    def deco(fn: Callable[[str], MatchResult]) -> Callable[[str], MatchResult]:
        _MATCHERS[name] = fn
        return fn
    return deco


# ---- procurement_term：采购期限两步判定（金标 v2，原型 v1.9.2 移植） ----
# 词表单一权威 = tools/m65/f12_regex_prototype.py（原型为设计期演示入口，
# 施工后正式测试调用本生产匹配器；两处口径以本文件为准，原型表迁移对齐）
_PROC_TERM_COMPOUNDS = r"(?:合同期限|履行期限|履行期|交付期限|供货服务期|供货期|服务期|租赁期限|租期|工期|委托期限)"
_PROC_NUMBER = r"(?:\d+|[零〇一二两三四五六七八九十百千万]+)"
_PROC_BASELINE = (
    _PROC_NUMBER + r"(?:日内|天内|个工作日内)"
    r"[^。，；,;\n\r]{0,20}(?:完成交付|完成送货|完成安装|完成验收|交付|送货|到场|送装|到货)"
)
_PROC_LABELED = re.compile(r"(?:^|[。；，,;\n\r])[^。，；,;\n\r]{0,12}" + _PROC_TERM_COMPOUNDS)
_PROC_NAME_SPAN = re.compile(r"[一-龥]{0,4}期限")
_PROC_NAME_LIST = ("合同期限", "履行期限", "履行期", "交付期限", "供货服务期", "供货期",
                   "服务期", "租赁期限", "租期", "工期", "委托期限")
_CLAUSE_SEPS = "。；，,;\n\r"


def _clause_of(text: str, start: int, end: int) -> str:
    lo = max((text.rfind(ch, 0, start) for ch in _CLAUSE_SEPS), default=-1)
    his = [text.find(ch, end) for ch in _CLAUSE_SEPS]
    his = [h for h in his if h != -1]
    hi = min(his) if his else len(text)
    return text[lo + 1:hi]


@_register_matcher("procurement_term")
def _match_procurement_term(text: str) -> MatchResult:
    """两步判定：白名单标签直接通过；无标签数字期限→采购动作链候选
    再核对分句「票头」——分句内 ××期限 不以采购白名单结尾 → 拒绝。"""
    m = _PROC_LABELED.search(text)
    if m:
        return MatchResult(True, m.start(), m.end(), m.group(), "whitelist")
    for m2 in re.finditer(_PROC_BASELINE, text):
        clause = _clause_of(text, m2.start(), m2.end())
        bad_head = any(not any(name.endswith(w) for w in _PROC_NAME_LIST)
                       for name in _PROC_NAME_SPAN.findall(clause))
        if not bad_head:
            return MatchResult(True, m2.start(), m2.end(), m2.group(), "unlabeled_numeric")
    return MatchResult(False, None, None, "", "none")


# ---- hit_alternatives：逐 occurrence 判定（breach 对等豁免门，金标 v2） ----
# 单一权威表在 YAML hit_alternatives 三元组；编译产物缓存于模块级 lru_cache
@functools.lru_cache(maxsize=32)
def _compile_hit_alts(key: tuple[tuple[str, str, bool], ...]
                      ) -> tuple[re.Pattern[str], list[bool], re.Pattern[str]]:
    """从 (pattern, start_token, neg_sensitive) 三元组编译：
    主命中正则（逐 alt 捕获组，neg 敏感 alt 焊否定前缀兜底 lookbehind）、
    否定敏感旗标表、禁行起点表。"""
    hit_alts: list[str] = []
    neg_flags: list[bool] = []
    starts: list[str] = []
    for pat, start_token, neg_sensitive in key:
        neg_flags.append(neg_sensitive)
        starts.append(start_token)
        # 否定判定单一权威=运行期 _contextual_negated（懂双重否定「不得不」不构成
        # 保护）；此处不得再焊编译期否定 lookbehind——它不认双重否定，会把该触发
        # 的命中整段吞掉（hit=None 连运行期判定的机会都没有）
        hit_alts.append(f"({pat})")
    # 旗标与旧 any_of 编译对齐（IGNORECASE|DOTALL）——默认行为不变红线：
    # 差一个 DOTALL，`免除.{0,8}违约` 的跨句命中（four_risk「完全免除。\n三、违约约定」）
    # 就会消失，breach 被 pass 词表接走 → 漏报回退
    hit_re = re.compile("|".join(hit_alts), flags=re.IGNORECASE | re.DOTALL)
    start_re = re.compile(r"(?:" + "|".join(dict.fromkeys(starts)) + r")",
                          flags=re.IGNORECASE)
    return hit_re, neg_flags, start_re


def _contextual_negated(text: str, start: int) -> bool:
    """occurrence 前方 6 字符否定判定：不|未 + 连接字符（能可予应急再得当为会）
    或 并未/不视为；双重否定（不得不…）不构成否定。"""
    prefix = text[max(0, start - 6):start]
    if re.search(r"(?:不|未)[能可予应急再得当为会]*$", prefix):
        return False if re.search(r"(?:不|未)[能可予应急再得当为]*(?:不|未)[能可予应急再得当为]*$", prefix) else True
    return prefix.endswith(("并未", "不视为"))


def _first_unprotected_hit_alt(text: str, rule: dict[str, Any]) -> Optional[dict[str, Any]]:
    """hit_alternatives 逐 occurrence 判定（对等豁免门）：
    否定上下文（仅 neg_sensitive）→ 对等豁免链（量词→禁行→免责事由→禁行→命中，
    链必须覆盖本次命中区间=occurrence 锚定；同句硬边界=。\n\r；分号不断链）；
    豁免搜索窗口化 unless_window（默认 320）。返回首个无保护命中或 None。"""
    alts = tuple((a.get("pattern") or "", a.get("start_token") or "",
                  bool(a.get("neg_sensitive"))) for a in rule.get("hit_alternatives") or [])
    if not alts:
        return None
    hit_re, neg_flags, start_re = _compile_hit_alts(alts)
    chain = re.compile(
        r"(?:任何一方|双方均?|各自|彼此|遇有不可抗力的一方)"
        + r"(?:(?!" + start_re.pattern + r")[^。\n\r]){0,150}"
        + r"(?:不可抗力|情势变更|政府行为|自然灾害|疫情)"
        + r"(?:(?!" + start_re.pattern + r")[^。\n\r]){0,80}"
        + r"(?:" + "|".join(f"({a.get('pattern') or ''})" for a in rule.get("hit_alternatives") or []) + r")",
        flags=re.IGNORECASE | re.DOTALL)
    window = int(rule.get("unless_window") or 320)
    for m in hit_re.finditer(text):
        s, e = m.span()
        alt_idx = (m.lastindex or 1) - 1
        if neg_flags[alt_idx] and _contextual_negated(text, s):
            continue
        w0 = max(0, s - window - 20)
        w1 = min(len(text), e + window + 20)
        # finditer 带 pos/endpos 时 match.span() 已是全文绝对索引，
        # 不得再叠 w0（叠了=窗口化一深就永不锚定，豁免门整体失效）
        anchored = any(mm.start() <= s and mm.end() >= e
                       for mm in chain.finditer(text, w0, w1))
        # unless 显式保护（文本级，如「免责事由但迟延履行…不免除」的句内否定变体）
        lo, hi = max(0, s - int(rule.get("unless_window") or 60)), min(len(text), e + 20)
        if rule.get("unless") and _negative_evidence(text[lo:hi], rule):
            continue
        if not anchored:
            return {"start": s, "end": e, "text": m.group(0),
                    "pattern": "hit_alternatives", "hit_type": "unprotected"}
    return None


def _run_matcher(text: str, rule: dict[str, Any]) -> MatchResult:
    """按规则引用的注册名执行匹配器；未知名字 fail-closed 抛错
    （load_checklist 校验链已在配置加载阶段拦截，此处为直调防御路径）。"""
    name = str(rule["matcher"])
    fn = _MATCHERS.get(name)
    if fn is None:
        raise ValueError(f"未知匹配器 matcher={name!r}（可用：{sorted(_MATCHERS)}）")
    return fn(text)


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
    # 读时校验与启动/CI 同源（外部审计二轮 PR-D 合一）：完整品类校验
    _validate_category_config(category, cfg)
    _validate_matchers(path, cfg)
    return cfg


def _validate_matchers(path: Path, cfg: dict[str, Any]) -> None:
    """matcher 引用校验（设计稿 v1.9.2：未知匹配器必须在加载配置阶段
    明确报错，错误信息含配置文件/检查项/matcher 名；禁止静默不匹配）。"""
    bad: list[str] = []
    for item in cfg.get("items", []):
        iid = str(item.get("id") or "?")
        for phase in ("need_attention", "pass"):
            for n, rule in enumerate((item.get("rules") or {}).get(phase) or []):
                if isinstance(rule, dict) and "matcher" in rule and rule["matcher"] not in _MATCHERS:
                    bad.append(f"{path}/{iid}#{phase}[{n}] matcher={rule['matcher']!r}")
                # hit_alternatives 三元组缺一不可（fail-closed：缺 start_token
                # 会让豁免链禁行段失明；缺 neg_sensitive 会让否定判定口径漂移）
                if isinstance(rule, dict) and "hit_alternatives" in rule:
                    for k, alt in enumerate(rule.get("hit_alternatives") or []):
                        if not (alt.get("pattern") and alt.get("start_token")
                                and isinstance(alt.get("neg_sensitive"), bool)):
                            bad.append(f"{path}/{iid}#{phase}[{n}].hit_alternatives[{k}] "
                                       f"缺 pattern/start_token/neg_sensitive 之一")
    if bad:
        raise ValueError(
            "配置错误（fail-closed）：匹配器/hit_alternatives 三元组结构非法\n" + "\n".join(bad))


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
    """启动/CI 校验（外部审计批2-④；二轮 PR-D 合一）：全部品类走同一套
    完整校验（segment 映射 + 规则正则 + item id，即 _validate_category_config）
    ——不再「load 时查一半、启动时查另一半」。配置坏了宁可起不来，
    也不要悄悄降级漏审。"""
    problems: list[str] = []
    for cat_info in list_categories():
        category = cat_info["id"]
        cfg = _load_yaml(CONFIG_DIR / f"checklist_{category}.yaml")
        try:
            _validate_category_config(category, cfg)
        except ValueError as exc:
            problems.append(str(exc))
    if problems:
        raise ValueError(
            "checklist 配置校验失败（fail-closed）：\n- " + "\n- ".join(problems)
        )


def _validate_category_config(category: str, cfg: dict[str, Any]) -> None:
    """单品类完整校验（外部审计二轮 PR-D 合一入口）：

    1. item id 非空且不重复；2. 规则正则全部可编译（含 unless/none_of）；
    3. segment 映射（_validate_segment_mapping：分段 key 不重复、item.segment
    指向存在分段）。load_checklist 读时与 validate_checklist_configs
    启动/CI 同源，规则写坏在任何入口都 fail-closed。"""
    problems: list[str] = []
    seen_ids: set[str] = set()
    for item in cfg.get("items", []):
        iid = str(item.get("id") or "")
        if not iid:
            problems.append(f"{category}: 存在无 id 的 item")
            continue
        if iid in seen_ids:
            problems.append(f"{category}: item id 重复 {iid}")
        seen_ids.add(iid)
        for phase in ("need_attention", "pass"):
            for rule in (item.get("rules") or {}).get(phase) or []:
                problems.extend(_regex_problems(category, iid, rule))
    if problems:
        raise ValueError("\n".join(problems))
    _validate_segment_mapping(cfg, category)
    _validate_rule_classes(category, cfg)


def _validate_rule_classes(category: str, cfg: dict[str, Any]) -> None:
    """三分法 class 校验（外审批 2，老钱裁定书第六节红线）：

    显式标注的 class 只允许 hardline/existence/heuristic；拼错（如 hardlin）
    **启动/加载即失败**，绝不静默回落 heuristic——那是「铁律线被拼错成启发式、
    AI 获得异议资格」的制度性漏洞。未标注仍是合法默认 heuristic（裁定书口径）。"""
    bad: list[str] = []

    def _check(owner: str, raw: Any) -> None:
        if raw is None:
            return
        val = str(raw).strip().lower()
        if val not in _VALID_RULE_CLASSES:
            bad.append(f"{category}/{owner}: class={raw!r} 非法（允许：{sorted(_VALID_RULE_CLASSES)}）")

    for item in cfg.get("items", []):
        iid = str(item.get("id") or "?")
        _check(f"{iid}(item)", item.get("class"))
        for phase in ("need_attention", "pass"):
            for n, rule in enumerate((item.get("rules") or {}).get(phase) or []):
                _check(f"{iid}#{phase}[{n}]", rule.get("class") if isinstance(rule, dict) else None)
    if bad:
        raise ValueError("品类 class 配置错误（fail-closed，修订须走裁定书第六节通道）：\n" + "\n".join(bad))


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


_VALID_RULE_CLASSES = {"hardline", "existence", "heuristic"}


def _rule_class(rule: dict[str, Any], item: dict[str, Any]) -> str:
    """三分法类别（路线图宪章，异议层受理分流依据）。
    优先级与裁定书（docs/stage3-class-opinion.md 第三节）对齐：
    命中规则自带 class（规则级）> 簇级 class（经 item 透传）> 默认 heuristic（未标注）。
    「未标注 → 默认 heuristic」是裁定书合法语义，保留；「非法显式值静默回落」
    旧兜底已删（三轮审计 G：与 fail-closed 制度不一致）——非法值由
    load_checklist 校验链拦截，直调防御路径下透传原值，消费端永不送审。"""
    raw = str(rule.get("class") or item.get("class") or "").strip().lower()
    # 未标注（falsy）= 合法默认；非法显式值透传（消费端 _eligible_direction 不送审）
    return raw if raw else "heuristic"


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
    for n, rule in enumerate(rules.get("need_attention") or []):
        evidence = _first_unprotected_occurrence(text, rule)
        if evidence:
            if pass_fulltext_fallback and _pass_matches(rules, text):
                break  # 补全信息在全文存在：完备型误报，交由 pass 定「通过」
            return {
                **base,
                "status": STATUS_ATTENTION,
                "note": rule.get("note", "需关注"),
                "quote": _quote_from_evidence(text, evidence),
                "hits": _hits_for_rule(text, rule, evidence),
                "evidence_start": evidence["start"],
                "evidence_end": evidence["end"],
                # 阶段 3 异议层地基（阶段 0 P3 承诺）：簇 id + 三分法类别透传
                "rule_id": f"{item['id']}#r{n}",
                "rule_class": _rule_class(rule, item),
            }

    # 2) pass
    for n, rule in enumerate(rules.get("pass") or []):
        evidence = _first_unprotected_occurrence(text, rule)
        if evidence:
            return {
                **base,
                "status": STATUS_PASS,
                "note": rule.get("note", "条款基本可接受"),
                "quote": _quote_from_evidence(text, evidence),
                "hits": _hits_for_rule(text, rule, evidence),
                "evidence_start": evidence["start"],
                "evidence_end": evidence["end"],
                "rule_id": f"{item['id']}#p{n}",
                "rule_class": _rule_class(rule, item),
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
        # 未找到=「有没有写 X」未命中：item 级类别（existence 判定的落点）
        "rule_id": None,
        "rule_class": _rule_class({}, item),
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

    F-1/F-2 第一批新增两类规则体（设计稿 v1.9.2）：
    - matcher：预注册匹配器一次匹配返回 MatchResult，状态/证据/引用全部
      从同一份结果派生（四处复用，不重扫）；
    - hit_alternatives：逐 occurrence 判定（否定上下文→对等豁免链锚定），
      evidence 携带 hit_type（unprotected）供引用层区分来源。
    """
    if "matcher" in rule:
        res = _run_matcher(text, rule)
        if not res.matched or res.start is None or res.end is None:
            return None
        return {"start": res.start, "end": res.end, "text": res.evidence,
                "pattern": f"matcher:{rule['matcher']}", "hit_type": res.hit_type}
    if "hit_alternatives" in rule:
        return _first_unprotected_hit_alt(text, rule)
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
    """Positive match: matcher / pattern / any_of (OR) / all_of (AND) / hit_alternatives."""
    if "matcher" in rule:
        return _run_matcher(text, rule).matched
    if "hit_alternatives" in rule:
        return _first_unprotected_hit_alt(text, rule) is not None
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



def _hits_for_rule(text: str, rule: dict[str, Any], evidence: dict[str, Any]) -> list[str]:
    """命中列表：matcher/hit_alternatives 路径复用证据对象（一次匹配多处复用，
    不重扫——外审 v1.9.2「匹配一次，多处复用」）；其余路径维持原提取。"""
    if evidence.get("hit_type"):
        frag = (evidence.get("text") or "")[:40].strip()
        return [f"{frag}({evidence['hit_type']})"] if frag else []
    return _extract_hits_from_rule(text, rule)


def _extract_hits_from_rule(text: str, rule: dict[str, Any]) -> list[str]:
    """Return matched substrings from the contract for positive rule patterns."""
    # matcher / hit_alternatives：复用判定入口的结果（一次匹配多处复用，不重扫）
    if "matcher" in rule:
        res = _run_matcher(text, rule)
        return ([f"{res.evidence[:40]}({res.hit_type})"] if res.matched else [])
    if "hit_alternatives" in rule:
        ev = _first_unprotected_hit_alt(text, rule)
        return ([f"{(ev['text'] or '')[:40]}({ev.get('hit_type', 'unprotected')})"]
                if ev else [])
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



