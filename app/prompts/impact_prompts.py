# -*- coding: utf-8 -*-
"""M7 连带影响提示词（B7-2b）。impact 只做定位与说明，不生成新条款文本。"""
from __future__ import annotations

PROMPT_VERSION = "impact-v1"

# 固定重审提示（报告尾部必带——选定草案≠解决问题，老板口径）
RE_REVIEW_NOTICE = ("选定草案≠解决问题：请以修订后的合同文本重新发起审查验证，"
                    "以确认修改效果与连带一致性。")


def build_system() -> str:
    return """你是合同连带影响分析辅助。任务：给定一份选定草案（对某条款的改写文本）与合同全文中可能引用/联动该条款的其他条款原文，逐条判断是否存在需要提示用户注意的不一致。

硬性约束：
1. 你只做定位与一致性说明，不生成新条款文本，不构成法律意见。
2. 只针对给定摘录逐条分析；没有把握的不要编造；发现确实存在不一致的，说明冲突点。
3. 严格只输出 JSON：{"findings": [{"index": 0, "note": "该条与草案中「…」的约定存在不一致：…"}]}
index 必须是给定摘录的编号；没有不一致时 findings 为空数组。"""


def build_user(draft_text: str, excerpts: list[dict]) -> str:
    lines = [f"选定草案（改写后的条款文本）：\n{draft_text}\n", "合同全文中的相关条款摘录：\n"]
    for e in excerpts:
        lines.append(f"[{e['index']}] {e['excerpt']}（位置≈第 {e['position']} 字符处）\n")
    lines.append("\n请逐条判断与选定草案是否存在不一致，按系统约束输出 JSON。")
    return "".join(lines)
