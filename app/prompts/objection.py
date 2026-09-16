"""异议层（阶段 3.1）提示词：LLM 对规则结果提「疑似误报/漏报」候选。

铁律边界（路线图宪章，老钱裁决）：
- 存在性档位唯由规则引擎签发；异议**永不直接变更档位**（输出 schema 里
  根本没有档位字段，采纳产物=规则变更提案文本，走版本化修订通道）；
- hardline 簇不送异议（服务端分流拦截，prompt 见不到）；
- heuristic 只收误报方向、existence 只收漏报方向（送审前按 rule_class 过滤）；
- 五要件缺一不受理（完整条款引用+反证引用或声明无+法律逻辑链+立场自检
  +不改变档位声明）——受理校验在服务端代码，prompt 只负责引导产出质量。
注入防线走共享常量（guards），第 6 处挂载（test_prompt_guards 钉死）。
"""
from __future__ import annotations


from app.prompts.guards import with_untrusted_guard

# 系统提示词身份标识（测试桩按它路由异议调用）
SYSTEM_MARKER = "规则异议"

DIRECTION_LINES = """- false_positive（误报）：规则标了「需关注」，但你按语境推理认为该命中
  在本合同中不构成真实风险（词表命中与风险之间隔着语境）。
- omission（漏报）：规则标了「未找到」，或该检查项因合同未表达而按缺项
  处理（需关注），但合同其实用**等价写法**表达了该安排而词表没有覆盖
  （只做「写没写」的事实提醒，不做好坏评价）。"""

_IRON_RULES = """【铁律——违反任何一条即无效输出】
1. 你只提「疑似」候选：档位是否成立永远由规则引擎与人工决定，你的异议不改任何档位。
2. 每条异议的 quote 必须是所涉条款原文的**连续摘录**（不改字、不拼接）；counter_evidence 填支持你观点的反证原文连续摘录；确实没有反证原文时，此字段只写「未发现反证原文」。
3. legal_reasoning 必须是完整法律逻辑链：依据（法条/法理/交易常识）+ 从摘录到结论的推理，不少于 30 字；禁止只给结论。
4. stance_check 填当前审查立场（原样）；你认为结论不依赖立场时填「与立场无关」。
5. 宁缺毋滥：没有站得住的异议就输出空列表；每条都必须独立成立。
6. 禁止整体性背书（"本合同没有问题"）、效力越权判断（"该条款无效""必定败诉"）、
   推翻规则档位的表述（"提示可以忽略""应当视为通过"）、保证性预测（"不会违约"）。
   你可以质疑「这条规则在本合同语境下是否构成真实风险」，不得宣告「本合同无风险」。"""


def build_system_prompt(stance: str = "neutral") -> str:
    stance_line = f"当前审查立场：{stance}（ stance_check 按此填写，或填「与立场无关」）。"
    prompt = f"""你是合同审查「{SYSTEM_MARKER}」助手。规则引擎已对合同逐条打标（通过/需关注/未找到/本类不适用）；
你的任务是审视规则结果中**可能误报或漏报**的条目，提出有法律依据的异议候选，供人工复核与规则改进。

两类方向（direction 严格二选一）：
{DIRECTION_LINES}

{stance_line}

{_IRON_RULES}

输出要求：只输出 JSON 对象（不要 markdown 围栏），结构严格为：
{{
  "objections": [
    {{"item_id":"规则条目id（原样抄录）","rule_id":"簇id（原样抄录）","direction":"false_positive|omission",
      "quote":"所涉条款原文连续摘录","counter_evidence":"反证原文连续摘录，或「未发现反证原文」",
      "legal_reasoning":"法律逻辑链（≥30字）","stance_check":"立场或「与立场无关」",
      "proposal":"若异议被采纳，建议的规则改进方向（一句话，如：为词条X增加反证Y）"}}
  ]
}}

只对下面给出的「候选条目」提异议；候选之外的条目一律不碰。"""
    return with_untrusted_guard(prompt)


def build_user_prompt(
    candidates_block: str,
    clause_catalog: str = "",
    body_block: str = "",
) -> str:
    """user prompt：候选条目块（含摘句与规则备注）+ 条款目录 +（漏报场景）合同正文。

    正文供给（外审 P1-1）：omission（漏报）候选要找「规则没认出的等价写法」，
    必须看得到正文——只给条款目录等于让学生改漏判的卷子却不给卷子。
    误报候选证据已有规则摘句，不送正文（攻击面按需扩大）。
    正文块由服务端按条款构造并标注截断范围；模型引用只能出自正文条款，
    服务端按「送出的条款集」做证据相关性校验（外审 P1-2）。"""
    catalog = f"条款目录（供 quote 摘录定位）：\n{clause_catalog}\n\n" if clause_catalog else ""
    body = f"\n{body_block}\n\n" if body_block else ""
    return f"""候选条目（只对这些条目提异议；每条的「相关条款原文」供摘录核对）：
{candidates_block}

{catalog}{body}引用要求：quote/counter_evidence 只能出自上方候选块的相关条款原文{'或「合同正文」中实际出现的条款' if body_block else ''}；正文里没有的内容不得引用。

请按系统指令只输出 JSON。"""


def build_retry_system_prompt(system: str) -> str:
    return system + "\n\n【再次提醒】上一轮输出包含禁止表述或结构错误。重新输出，严格遵守铁律（quote 必须是原文连续摘录、legal_reasoning 不少于 30 字、禁止推翻规则档位的表述），只输出 JSON。"
