"""质量层（阶段 2.1 AI 质量分析）提示词。

定位（路线图铁律 5）：质量层是规则清单之外的参考分析——参谋不是裁判。
- 条目不计入评分卡、不改变任何规则档位（档位唯由规则引擎签发）；
- 每条必须带合同原文连续摘录，无原文依据的观察一律不输出；
- 所有条目均为「待人工确认」，模型无权声明某条不用确认（该字段根本
  不在输出 schema 里，由代码强制 True）。
注入防线走共享常量（app/prompts/guards.py），test_prompt_guards 钉挂载。
"""
from __future__ import annotations

from typing import Any, Optional

from app.prompts.guards import with_untrusted_guard
from app.services.scorecard_prompts import _rule_block

# 系统提示词里的身份标识（测试桩按它路由质量层调用）
SYSTEM_MARKER = "AI 观察"

DIMENSIONS = ("completeness", "consistency", "impact")

DIMENSION_LINES = """- completeness（完整性）：应有而缺失、或表述空悬无法执行的安排（与规则「未找到」互补；你只做观察，不判档位）。
- consistency（一致性）：条款之间互相矛盾（如付款安排与验收条件冲突、期限与违约责任对不上、定义与实际使用不一致）。
- impact（影响）：条款本身写得通，但按用户立场通读会吃亏的实际后果。"""

# 主调用/一致性轮共用的铁律段
_IRON_RULES = """【铁律——违反任何一条即无效输出】
1. 你产出的观察只是参考信息：不计入评分、不改变规则引擎的任何档位结论。
2. 每条观察的 quote 必须是合同原文的**连续摘录**（可跨句但不得改字、不得拼接不相邻文字）；没有原文依据的观察一律不要输出。
3. 最多 12 条观察（每个维度不超过 6 条），宁缺毋滥；没有值得说的就输出空列表。
4. 禁止整体性背书（如"本合同没有问题""可以放心签署"）、效力越权判断（如"该条款无效""必定败诉"）、推翻规则档位的表述（如"提示可以忽略"）、保证性预测（如"不会违约"）。
5. 每条 comment 两句话结构：前半句说实际影响，后半句说修改建议。"""


def build_system_prompt(policies: list[str]) -> str:
    policy_block = "\n".join(f"- {p}" for p in policies) or "- （无额外政策）"
    prompt = f"""你是合同审查「{SYSTEM_MARKER}」助手。规则引擎已对合同逐项打标（通过/需关注/未找到/本类不适用）；你的任务是在规则清单之外，从三个维度补充规则词表覆盖不到的参考观察。

三个维度（dimension 取值严格三选一）：
{DIMENSION_LINES}

政策参考（为什么这样看）：
{policy_block}

{_IRON_RULES}

输出要求：只输出 JSON 对象（不要 markdown 围栏），结构严格为：
{{
  "observations": [
    {{"dimension":"completeness|consistency|impact","title":"≤30字小标题","quote":"合同原文连续摘录","clause_id":"c05 或 null","comment":"≤120字：前半句实际影响，后半句修改建议"}}
  ]
}}

clause_id 填条款目录里的编号；拿不准就填 null，不要编造编号。"""
    return with_untrusted_guard(prompt)


def build_user_prompt(
    text: str,
    items: list[dict[str, Any]],
    clause_index: Optional[dict[str, Any]] = None,
) -> str:
    """主调用 user prompt：规则打标块 + 条款目录 + 合同全文。"""
    catalog = _clause_catalog(clause_index)
    catalog_block = f"条款目录（clause_id 供引用）：\n{catalog}\n\n" if catalog else ""
    return f"""规则引擎打标结果：
{_rule_block(items)}

{catalog_block}合同全文：
{text}

请按系统指令只输出 JSON。"""


def build_consistency_user_prompt(rule_block: str, material_block: str) -> str:
    """一致性轮 user prompt：**不含合同全文**（延迟护栏对齐评分 reduce 先例）。

    素材块由调用方用已通过 quote 全文校验的观察构造，并已过禁语清洗；
    此处明示 quote 只能从素材中原样摘录——伪造原文在这里没有原文可抄。
    """
    return f"""规则引擎打标结果：
{rule_block}

分段阅读观察素材（已核实均出自合同原文）：
{material_block}

【一致性轮】请基于以上素材只输出 JSON：observations 数组（结构与主调用一致），
且只做 consistency（一致性）维度的观察；quote 只能从素材中已有的原文摘录里
原样引用；没有跨条款矛盾就输出 {{"observations":[]}}。"""


def build_retry_system_prompt(system: str) -> str:
    return system + "\n\n【再次提醒】上一轮输出包含禁止表述或结构错误。重新输出，严格遵守铁律（quote 必须是原文连续摘录、禁止整体性背书、dimension 三选一），只输出 JSON。"


def _clause_catalog(clause_index: Optional[dict[str, Any]]) -> str:
    clauses = (clause_index or {}).get("clauses") or []
    lines = []
    for c in clauses:
        cid = str(c.get("id") or "").strip()
        if cid:
            lines.append(f"- {cid} {str(c.get('heading') or '').strip()}")
    return "\n".join(lines)
