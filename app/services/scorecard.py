"""M3.5 model scorecard (百分制评分卡).

Iron rules (docs/m3.5-legal-scorecard-opinion.md):
- Score is advisory only; NEVER mutates rule statuses. 金标只认规则档.
- Caps & deduction floors enforced HERE in code post-processing —
  never trusted to the model (第三节.4 / 第五节).
- Forbidden phrases (config/scorecard_forbidden.yaml) scrubbed;
  repeat hit → degrade to score-only.
- No key / no rule results → unavailable with explicit reason.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Optional

import yaml

from app.services.checklist import (
    STATUS_ATTENTION,
    STATUS_NA,
    STATUS_NOT_FOUND,
    STATUS_PASS,
    load_checklist,
)

logger = logging.getLogger(__name__)

CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"
FORBIDDEN_PATH = CONFIG_DIR / "scorecard_forbidden.yaml"

# 展示档位（纯参考，非规则状态；文案九哥定稿 2026-09-05）
TIERS = [
    # 90+ 档措辞不盖章、不省略人工动作（肉饼审查 P2-1：与 A 类禁语同向的表述系统自己也不许用；
    # 外部审计：「基本没毛病」对关键词初筛系统仍偏强，降调）
    {"min": 90, "label": "未见实质风险", "hint": "这版没发现实质风险，逐条结果请照常过一遍"},
    {"min": 70, "label": "有几处要留心", "hint": "多是能补正的地方，谈一谈再签更稳"},
    {"min": 50, "label": "有实质风险", "hint": "核心条款有硬伤，先改完再谈签的事"},
    {"min": 0, "label": "风险很大", "hint": "多处理念都偏了，先缓一缓（找人看看再定）"},
]

# 硬性封顶（老钱意见书第三节）
CAP_ANY_ATTENTION = 89
CAP_CORE_ATTENTION = 74  # B/D 段存在需关注
CORE_SEGMENTS = {"B", "D"}
# 扣分下限：段内每个「需关注」扣 ≥ 段权重40%；「未找到」扣 ≥ 60%
DEDUCTION_ATTENTION = 0.4
DEDUCTION_NOT_FOUND = 0.6

# 评分提示词的单次文本上限。2026-09-07 线上事故：12000 字 + 评分指令让 glm-5.2
# 生成超 240s（同步上传被拖死）。压到 6000 后评分延迟回到 ~30-60s 量级；
# 规则引擎不受此限（checklist 全文扫描，截断只影响参考层评分提示词）。
# 截断策略（外部审计修正）：**头 + 尾**——主体信息集中在头部，签字/印章/落款
# 常在尾部，只取头部会漏掉签署区信号。总预算仍为 MAX_CONTRACT_CHARS。
# 待法务老钱追认：评分是纯参考层，头尾采样足够定调
# 阶段 1.2：超过上限的合同不再整体截断——走分段阅读（model_review map-reduce），
# 每个阅读块仍受本常量约束（延迟教训直接复用）；_clip_for_scoring 保留为
# 追问（/api/ask）与分段回退路径的共用截断。
MAX_CONTRACT_CHARS = 6000
_TAIL_CHARS = 1000
_CLIP_MARKER = "\n…(中段截断)…\n"

# D 类反向必备项：免责句（九哥定稿 2026-09-05）
DISCLAIMER = "以上都是机器给的参考意见，签之前建议找懂行的人再看一眼。"

_forbidden_cache: Optional[dict[str, list[str]]] = None


def load_scorecard_config(category: str) -> dict[str, Any]:
    """Read `scorecard:` block (segments/weights) from the category checklist YAML."""
    cfg = load_checklist(category)
    sc = cfg.get("scorecard") or {}
    segments = [
        {
            "key": s.get("key", ""),
            "name": s.get("name", s.get("key", "")),
            "weight": int(s.get("weight", 0)),
            "na": bool(s.get("na", False)),
        }
        for s in sc.get("segments", [])
    ]
    return {"segments": segments}


def load_forbidden() -> dict[str, list[str]]:
    global _forbidden_cache
    if _forbidden_cache is None:
        try:
            with FORBIDDEN_PATH.open("r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            _forbidden_cache = {
                k: [str(w) for w in v]
                for k, v in (data.get("forbidden") or {}).items()
                if isinstance(v, list)
            }
        except OSError:
            logger.warning("scorecard_forbidden.yaml missing; no forbidden list")
            _forbidden_cache = {}
    return _forbidden_cache


def check_forbidden(text: str) -> list[str]:
    """Return forbidden phrases found in text (A/C classes matter most).

    否定感知（小智娘租赁测试 P2）：命中词紧邻前缀是否定时不算违规——
    「并不能说明这份合同无效」是对风险的正当提示，不能被清洗成【已过滤】
    反向洗掉审查话术。子串命中 + 前缀非否定才记违规。
    """
    if not text:
        return []
    hits: list[str] = []
    for _cat, words in load_forbidden().items():
        for w in words:
            if w in text and w not in hits and not _negated_at(text, w):
                hits.append(w)
    return hits


# 命中词前 6 字窗口含这些否定表述时，视为对禁语的否定引用而非违规。
# 注意：「没有」「不是」不收——它们过短且「没有问题」本身是禁语，会互相干扰漏检。
_NEGATION_PREFIXES = (
    "不能", "不会", "无法", "并非", "并不是", "不存在", "不构成", "不代表",
    "不等于", "未见", "未必", "未发现", "未出现", "不属", "并非没有", "并无",
)


def _negated_at(text: str, word: str) -> bool:
    """词在文中的每次出现，只要存在非否定上下文的实例即算真命中。

    否定词允许与命中词间隔少量修饰（「并不能说明…无效」），取命中词前 6 字窗口。
    """
    start = 0
    while True:
        idx = text.find(word, start)
        if idx < 0:
            return True  # 所有出现都被否定前缀覆盖
        window = text[max(0, idx - 6):idx]
        if not any(p in window for p in _NEGATION_PREFIXES):
            return False
        start = idx + 1


def scrub_forbidden(text: str) -> str:
    """Last-resort scrub: replace forbidden phrases with a filter marker."""
    if not text:
        return text
    out = text
    for w in check_forbidden(out):
        out = out.replace(w, "【已过滤】")
    return re.sub(r"(【已过滤】)+", "【已过滤】", out)


def has_rule_results(items: Optional[list[dict[str, Any]]]) -> bool:
    """评分出入门禁：有规则结果才出分."""
    return bool(items)


def tier_of(total: int) -> dict[str, Any]:
    for t in TIERS:
        if total >= t["min"]:
            return {"label": t["label"], "hint": t["hint"]}
    return {"label": TIERS[-1]["label"], "hint": TIERS[-1]["hint"]}


# ---------- prompt building (merged single call lives in model_review) ----------

_CONSISTENCY_CLAUSES = """【规则结果优先条款】本评分卡是规则引擎打标结果的解释与汇总，不是独立的第二次审查。你必须遵守：
1. 规则已标「需关注」「未找到」「通过」的，你的评语与分数必须与该档位方向一致。你不得降级、弱化、推翻或"补充解释掉"任何规则档位；确有不同看法，只能以"此外提示"方式附加，且不得降低该项所在段的扣分。
2. 凡存在任一「需关注」或「未找到」项的段，该段不得给满分，评语必须如实点名该项。
3. 不得对规则档位做对冲表述（如"虽有提示但问题不大"）。认为风险轻微，表述上限是"该项属可补正的形式瑕疵，修订成本低"，扣分仍会由系统强制执行。
4. 总分、分段分与评语三者必须自洽：列举了 N 个问题就体现 N 次扣分；不允许"列举五个问题、总分92"。
5. 冲突时一律以规则档位为准，并按规则档位方向措辞。
6. 禁止出现整体性背书（如"本合同没有问题""可以放心签署"）、效力越权判断（如"该条款无效""必定败诉"）、推翻规则档位的表述（如"提示可以忽略"）、替代性声明（如"可替代律师审查"）、保证性预测（如"不会违约"）。"""


def build_system_prompt(segments: list[dict[str, Any]], policies: list[str]) -> str:
    seg_lines = []
    for s in segments:
        na_note = "（本类不适用，给 0 分并注明）" if s.get("na") else f"（满分 {s['weight']} 分）"
        seg_lines.append(f"- {s['key']}：{s['name']} {na_note}")
    seg_block = "\n".join(seg_lines)
    policy_block = "\n".join(f"- {p}" for p in policies) or "- （无额外政策）"
    return f"""你是合同审查「评分卡」助手。规则引擎已对合同逐项打标（通过/需关注/未找到/本类不适用）；你的任务是**解释与汇总**规则结果，给出百分制评分卡。

{_CONSISTENCY_CLAUSES}

评分段（key：名称 满分）：
{seg_block}

政策参考（为什么这样标）：
{policy_block}

输出要求：只输出 JSON 对象（不要 markdown 围栏），结构严格为：
{{
  "scorecard": {{
    "summary": "一句话总评（≤60字，说人话，不盖章）",
    "segments": [
      {{"key":"A","score":整数,"comment":"该段一句话评语，须点名该段内的需关注/未找到项","gap_item_ids":["该段内表述弱/有缺口条目的id，没有则[]"]}}
    ]
  }},
  "candidates": [
    {{"item_id":"...","name":"...","note":"...","quote":"..."}}
  ]
}}

segments[].gap_item_ids 说明：该段内你认为「表述弱、有缺口、值得补盲」的条目 id（含规则已标「通过」但表述单薄的项），没有则输出 []。系统会把点名条目纳入定向补盲，供人工确认——这只是点名，不改规则档位。
candidates 说明：对「规则未标需关注、但你发现真实风险且能引用原文」的条目提出候选；quote 必须是合同原文连续摘录，没有原文依据就不要输出该项；无候选输出 []。禁止改写规则已有结论。"""


def _clip_for_scoring(text: str) -> str:
    """头 + 尾采样截断（总预算 MAX_CONTRACT_CHARS）：
    主体在头部、签署/落款在尾部，中段条款密集但信噪比低。"""
    if len(text) <= MAX_CONTRACT_CHARS:
        return text
    head = MAX_CONTRACT_CHARS - _TAIL_CHARS - len(_CLIP_MARKER)
    return text[:head] + _CLIP_MARKER + text[-_TAIL_CHARS:]


def clip_contract_text(text: str) -> str:
    """公开封装：发给大模型的合同文本统一走头尾采样（评分卡与追问共用）。"""
    return _clip_for_scoring(text)


def _rule_block(items: list[dict[str, Any]]) -> str:
    lines = []
    for it in items:
        na = "（本类不适用）" if it.get("category_na") else ""
        lines.append(
            f"- {it.get('name')}（id={it.get('id')}）：{it.get('status')}{na}"
            f"｜备注：{it.get('note') or '无'}"
        )
    return "\n".join(lines)


def build_user_prompt(text: str, items: list[dict[str, Any]]) -> str:
    body = _clip_for_scoring(text)
    rule_block = _rule_block(items)
    return f"""规则引擎打标结果：
{rule_block}

合同全文：
{body}

请按系统指令只输出 JSON。"""


# ---------- 阶段 1.2 分段阅读（map-reduce）：长合同消灭 6000 字近视 ----------

def build_map_system_prompt(policies: list[str]) -> str:
    policy_block = "\n".join(f"- {p}" for p in policies) or "- （无额外政策）"
    return f"""你是合同审查「分段阅读」助手。长合同被切成若干片段分批阅读，你只看到一个片段。任务：通读本片段，找出与规则清单相关的真实风险信号，产出**观察素材**。你不下结论、不打分、不改任何档位——汇总由另一轮完成。

政策参考（为什么这样找）：
{policy_block}

输出要求：只输出 JSON 对象（不要 markdown 围栏），结构严格为：
{{
  "observations": [
    {{"segment":"A","comment":"本片段内发现的具体风险点，须点明条款位置（如「第五条」）与关键表述","gap_item_ids":["表述弱/有缺口/值得补盲的清单项id"],"candidates":[{{"item_id":"...","name":"...","note":"...","quote":"合同原文连续摘录"}}]}}
  ]
}}

说明：
- segment 填该风险点所属的评分段 key；与清单无关的片段输出 {{"observations":[]}}。
- quote 必须是**本片段原文连续摘录**，没有原文依据就不要输出候选。
- 禁止整体性背书（如"没有问题""可以放心签署"）、禁止效力越权判断（如"该条款无效"）、禁止推翻规则档位的表述（如"提示可以忽略"）。"""


def build_map_user_prompt(chunk_text: str, items: list[dict[str, Any]], part_no: int, part_total: int) -> str:
    return f"""规则引擎打标清单：
{_rule_block(items)}

合同片段（第 {part_no}/{part_total} 部分）：
{chunk_text}

请按系统指令只输出 JSON。"""


def build_reduce_user_prompt(items: list[dict[str, Any]], observations_block: str) -> str:
    """汇总轮 user prompt：规则打标 + 分段观察素材，**不含合同全文**（延迟护栏：
    输入规模与单次调用路径同量级）。"""
    return f"""规则引擎打标结果：
{_rule_block(items)}

分段阅读观察（分片阅读产生的素材，仅供定位与展开；档位与扣分一律以规则打标为准，不得因素材改判）：
{observations_block}

请按系统指令只输出 JSON（输出结构与单文本版完全一致）。"""


def parse_map_payload(raw: str) -> Optional[list[dict[str, Any]]]:
    """解析 map 轮输出。成功返回 observations 列表（合法空 = []）；解析失败返回 None
    ——调用方据此区分「无风险」与「结构错误」，二者不能混为回退依据。"""
    if not raw:
        return None
    text = raw.strip()
    fence = re.match(r"^```(?:json)?\s*([\s\S]*?)\s*```$", text)
    if fence:
        text = fence.group(1).strip()
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{[\s\S]*\}", text)
        if not m:
            return None
        try:
            obj = json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
    obs = obj.get("observations") if isinstance(obj, dict) else None
    if not isinstance(obs, list):
        return None
    return [o for o in obs if isinstance(o, dict)]


# 观察素材进入 reduce prompt 前的尺寸护栏（防素材自身膨胀拖垮汇总轮延迟）
_OBS_COMMENT_MAX = 200
_OBS_CAND_NOTE_MAX = 100
_OBS_CAND_QUOTE_MAX = 150
_OBS_MAX_ENTRIES = 40


def format_observations(observations: list[dict[str, Any]]) -> str:
    """把各片段观察聚合成 reduce prompt 的素材块；进门先过禁语清洗（防禁语
    经素材回流放大重试）。"""
    lines: list[str] = []
    for i, obs in enumerate(observations[:_OBS_MAX_ENTRIES], start=1):
        comment = scrub_forbidden(str(obs.get("comment") or ""))[:_OBS_COMMENT_MAX]
        seg = str(obs.get("segment") or "")
        entry = f"【片段 {i}】{('[' + seg + '] ') if seg else ''}{comment}".rstrip()
        gap_ids = obs.get("gap_item_ids")
        if isinstance(gap_ids, list) and gap_ids:
            entry += "\n  点名缺口：" + "、".join(str(g) for g in gap_ids[:10])
        cands = obs.get("candidates")
        if isinstance(cands, list):
            for c in cands[:5]:
                if not isinstance(c, dict):
                    continue
                note = scrub_forbidden(str(c.get("note") or ""))[:_OBS_CAND_NOTE_MAX]
                quote = str(c.get("quote") or "")[:_OBS_CAND_QUOTE_MAX]
                entry += f"\n  候选：{c.get('item_id', '')}｜{c.get('name', '')}｜{note}｜原文：{quote}"
        lines.append(entry)
    return "\n".join(lines)


def build_review_chunks(
    text: str, clause_index: Optional[dict[str, Any]] = None, max_segments: int = 4
) -> list[str]:
    """长合同 → ≤max_segments 个阅读块（每块 ≤MAX_CONTRACT_CHARS，延迟护栏）。

    优先按条款索引对齐切块（条款不跨块）；无索引/坐标失效时按段落聚合回退。
    块数超限时尾部并成一块并走头尾采样截断——覆盖 4 块 ≈2.4 万字，超出部分
    是深度换延迟的既有取舍，显式截断好过静默假装读过。
    """
    text = text or ""
    if not text:
        return []
    ranges = _reading_ranges(text, clause_index)
    chunks: list[str] = []
    cur_start: Optional[int] = None
    cur_end = 0
    for start, end in ranges:
        size = end - start
        if size > MAX_CONTRACT_CHARS:
            # 单条款超限：冲刷当前块后按字符硬切
            if cur_start is not None:
                chunks.append(text[cur_start:cur_end])
                cur_start = None
            for i in range(start, end, MAX_CONTRACT_CHARS):
                chunks.append(text[i : min(end, i + MAX_CONTRACT_CHARS)])
            continue
        if cur_start is None:
            cur_start, cur_end = start, end
            continue
        if cur_end - cur_start + size > MAX_CONTRACT_CHARS:
            chunks.append(text[cur_start:cur_end])
            cur_start, cur_end = start, end
        else:
            cur_end = end
    if cur_start is not None:
        chunks.append(text[cur_start:cur_end])

    if len(chunks) > max_segments:
        tail = "".join(chunks[max_segments - 1 :])
        chunks = chunks[: max_segments - 1] + [_clip_for_scoring(tail)]
    return chunks


def _reading_ranges(text: str, clause_index: Optional[dict[str, Any]]) -> list[tuple[int, int]]:
    """阅读区间列表：条款对齐（索引有效）或段落聚合（回退）。区间有序且覆盖全文。"""
    clauses = (clause_index or {}).get("clauses") or []
    ranges = [
        (int(c["start"]), int(c["end"]))
        for c in clauses
        if isinstance(c.get("start"), int) and isinstance(c.get("end"), int)
        and 0 <= c["start"] < c["end"] <= len(text)
    ]
    if len(ranges) >= 1 and ranges == sorted(ranges):
        # 补上索引外的散落文本（开头 preamble 已由索引建档；这里兜底残余间隙）
        return ranges

    # 回退：按段落聚合到 MAX_CONTRACT_CHARS
    out: list[tuple[int, int]] = []
    pos = 0
    seg_start = 0
    for para in text.split("\n"):
        plen = len(para) + 1
        if pos - seg_start + plen > MAX_CONTRACT_CHARS and pos > seg_start:
            out.append((seg_start, pos))
            seg_start = pos
        pos += plen
    if seg_start < len(text):
        out.append((seg_start, len(text)))
    return out


# ---------- parsing & post-processing ----------

def _get_scorecard(obj: dict[str, Any]) -> Any:
    """兼容模型输出的键名大小写（ScoreCard / Scorecard 等）."""
    if "scorecard" in obj:
        return obj["scorecard"]
    for k, v in obj.items():
        if isinstance(k, str) and k.lower() == "scorecard":
            return v
    return None


def parse_model_payload(raw: str) -> Optional[dict[str, Any]]:
    """Parse the combined {scorecard, candidates} payload. None on failure."""
    if not raw:
        return None
    text = raw.strip()
    fence = re.match(r"^```(?:json)?\s*([\s\S]*?)\s*```$", text)
    if fence:
        text = fence.group(1).strip()

    def _accept(o: Any) -> Optional[dict[str, Any]]:
        if isinstance(o, dict):
            sc = _get_scorecard(o)
            if isinstance(sc, dict):
                o["scorecard"] = sc  # 归一化为小写键，下游 postprocess 统一读取
                return o
        return None

    try:
        obj = json.loads(text)
        accepted = _accept(obj)
        if accepted is not None:
            return accepted
    except json.JSONDecodeError:
        m = re.search(r"\{[\s\S]*\}", text)
        if m:
            try:
                obj = json.loads(m.group(0))
                accepted = _accept(obj)
                if accepted is not None:
                    return accepted
            except json.JSONDecodeError:
                return None
    return None


def postprocess(
    payload: dict[str, Any],
    items: list[dict[str, Any]],
    segments: list[dict[str, Any]],
) -> dict[str, Any]:
    """Code-enforced consistency: floors, caps, total recompute, scrubbing.

    The model's numbers are treated as an upper bound only — never trusted.
    """
    if not segments:
        # 旧品类缺 scorecard: 块：不出假分（否则恒 0 分 +「风险很大」误导用户）
        return unavailable("no_scorecard_config")

    sc = payload.get("scorecard") or {}
    model_segments = {
        str(s.get("key")): s for s in (sc.get("segments") or []) if isinstance(s, dict)
    }

    # items by segment (from checklist YAML `segment` field)
    seg_of_item = {str(it.get("id")): str(it.get("segment") or "") for it in items}
    status_of_item = {str(it.get("id")): it.get("status") for it in items}
    name_of_item = {str(it.get("id")): it.get("name") for it in items}

    any_attention = False
    core_attention = False
    out_segments: list[dict[str, Any]] = []
    caps_applied: list[str] = []
    all_attention_names: list[str] = []
    # 「未找到」比「需关注」更重，同触发封顶（肉饼审查 P2-1：否则带未找到的合同可能比带需关注的分还高）
    all_hard_names: list[str] = []

    for seg in segments:
        key = str(seg["key"])
        weight = int(seg["weight"])
        if seg.get("na") or weight <= 0:
            out_segments.append(
                {
                    "key": key,
                    "name": seg["name"],
                    "weight": 0,
                    "score": 0,
                    "comment": "本类不适用",
                    "na": True,
                }
            )
            continue

        model_seg = model_segments.get(key) or {}
        if "score" not in model_seg:
            # 沉默≠满分：模型漏报分段按 0 计（肉饼审计：不得向上偏置）
            score = 0
        else:
            try:
                score = int(round(float(model_seg["score"])))
            except (TypeError, ValueError):
                # 非数值与缺失同责：按 0 计（沉默≠满分，乱答也不给满分）
                score = 0
            score = max(0, min(weight, score))

        # 扣分下限（不依赖模型自觉）
        seg_items = [iid for iid, sk in seg_of_item.items() if sk == key]
        deduction = 0
        seg_attention_names: list[str] = []
        seg_hard_names: list[str] = []
        for iid in seg_items:
            st = status_of_item.get(iid)
            if st == STATUS_ATTENTION:
                # 口径备案（肉饼租赁审计 P2，2026-09-06）：missing_as=需关注 的
                # 加严显示档走本 0.4 扣分档，比默认「未找到」的 0.6 反而轻——
                # 显示档位与扣分强度耦合倒挂是既有设计，若要归一需法务拍板后统一调整
                deduction += DEDUCTION_ATTENTION * weight
                seg_attention_names.append(str(name_of_item.get(iid) or iid))
                seg_hard_names.append(str(name_of_item.get(iid) or iid))
            elif st == STATUS_NOT_FOUND:
                deduction += DEDUCTION_NOT_FOUND * weight
                seg_hard_names.append(str(name_of_item.get(iid) or iid))
        floor = max(0, int(round(weight - deduction)))
        score = min(score, floor)

        comment = str(model_segments.get(key, {}).get("comment") or "")
        if seg_hard_names and not comment:
            comment = "存在需关注/未找到项：" + "、".join(seg_hard_names)
        comment = scrub_forbidden(comment)

        out_segments.append(
            {
                "key": key,
                "name": seg["name"],
                "weight": weight,
                "score": score,
                "comment": comment,
                "na": False,
            }
        )

        if seg_hard_names:
            any_attention = True
            all_attention_names.extend(seg_attention_names)
            all_hard_names.extend(seg_hard_names)
            if key in CORE_SEGMENTS:
                core_attention = True

    total = sum(s["score"] for s in out_segments)

    # 硬性封顶（代码强制）；封顶消息点名全部硬伤条款（九哥定稿【XX】【YY】；P2-2：不只列核心段）
    cap: Optional[int] = None
    if any_attention:
        cap = CAP_ANY_ATTENTION
    if core_attention:
        cap = min(cap or 100, CAP_CORE_ATTENTION)
    if cap is not None and total > cap:
        total = cap
        name_block = "".join(f"【{n}】" for n in all_hard_names)
        caps_applied.append(
            f"因存在{name_block}需关注/未找到项，总分已按上限 {cap} 封顶（失分不能互相抵扣）"
        )

    summary = scrub_forbidden(str(sc.get("summary") or "")).strip()
    if not summary:
        summary = f"规则结果汇总评分 {total} 分。"

    return {
        "available": True,
        "reason": None,
        "total": total,
        "tier": tier_of(total),
        "summary": summary,
        "segments": out_segments,
        "caps_applied": caps_applied,
        "disclaimer": DISCLAIMER,
        "advisory_only": True,
    }


def naming_complete(final: dict[str, Any], items: list[dict[str, Any]]) -> bool:
    """评语须点名所有非「通过」项；漏点名 → 降级为仅展示分数（第五节.c)."""
    blob = final.get("summary") or ""
    for s in final.get("segments") or []:
        blob += str(s.get("comment") or "")
    for it in items:
        if it.get("status") in (STATUS_PASS, STATUS_NA):
            continue
        name = str(it.get("name") or "")
        if name and name not in blob:
            return False
    return True


def degrade_to_score_only(final: dict[str, Any]) -> dict[str, Any]:
    """Drop comments/summary, keep numbers + disclaimer (repeat forbidden hit)."""
    out = dict(final)
    for s in out.get("segments") or []:
        if isinstance(s, dict):
            s["comment"] = ""
    out["summary"] = f"规则结果汇总评分 {out.get('total', 0)} 分。"
    out["degraded"] = True
    return out


def unavailable(reason: str) -> dict[str, Any]:
    """Explicit unavailable payload — UI must show 未开通, never fake numbers."""
    return {
        "available": False,
        "reason": reason,
        "total": None,
        "tier": None,
        "summary": "",
        "segments": [],
        "caps_applied": [],
        "disclaimer": DISCLAIMER,
        "advisory_only": True,
    }
