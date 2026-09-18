"""统一 Coverage 口径（宪法 P0-D2：llm-position-authority 规范 23 节）。

背景（宪法一致性审计 B3）：此前三套形状分叉——scorecard（chunks_total/
reviewed/limited）、quality（近似 scorecard+dropped_count）、objection
（candidate/body 两维私有形状）——且 precheck/ask 两大 LLM 节点**零
coverage**（头尾采样不记账，违反宪法第十八条「覆盖范围必须诚实」）。

本模块定义公共 schema；各节点产出 unified dict（键集对齐规范 23 节）。
既有 API 的旧键（scorecard.limited / objection.body_limited 等）保持
向后兼容并存，新消费方一律读 unified 键。

字段（对齐规范 23 节）：
- original_chars：合同原文总字符
- chars_sent：实际发送给模型的合同字符数（不含 prompt 装饰）
- clause_ids_sent：实际发送的条款 id
- chunks_total / chunks_reviewed：分段阅读的分块账（非分段节点为 None）
- unread_ranges：未读区间 [(start, end), ...]（无法定位时为 None）
- truncation_reason：截断原因（sampling/truncation/budget；完整阅读为 None）
- limited：是否部分阅读（诚实位——「在我看到的范围内没找到」≠「合同中不存在」）
- node：产出节点（precheck/model_review/quality/objection/ask）
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class UnifiedCoverage:
    original_chars: int = 0
    chars_sent: int = 0
    clause_ids_sent: list[str] = field(default_factory=list)
    chunks_total: Optional[int] = None
    chunks_reviewed: Optional[int] = None
    unread_ranges: Optional[list[list[int]]] = None  # None=无法定位（诚实区分空列表）
    truncation_reason: Optional[str] = None
    limited: bool = False
    node: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "original_chars": self.original_chars,
            "chars_sent": self.chars_sent,
            "clause_ids_sent": list(self.clause_ids_sent),
            "chunks_total": self.chunks_total,
            "chunks_reviewed": self.chunks_reviewed,
            "unread_ranges": self.unread_ranges,
            "truncation_reason": self.truncation_reason,
            "limited": self.limited,
            "node": self.node,
        }


def head_tail_sampling_coverage(
    original_chars: int, sent_chars: int, node: str
) -> dict[str, Any]:
    """头尾采样节点（precheck/ask）的标准 coverage：明确「只读了头尾」。"""
    return UnifiedCoverage(
        original_chars=original_chars,
        chars_sent=min(sent_chars, original_chars),
        unread_ranges=None,  # 头尾采样的未读中段为离散区间，此处无法精确定位
        truncation_reason="head_tail_sampling" if original_chars > sent_chars else None,
        limited=original_chars > sent_chars,
        node=node,
    ).to_dict()


def full_read_coverage(original_chars: int, clause_ids: list[str], node: str) -> dict[str, Any]:
    """全文阅读节点（短合同直送全文）的标准 coverage。"""
    return UnifiedCoverage(
        original_chars=original_chars,
        chars_sent=original_chars,
        clause_ids_sent=list(clause_ids),
        limited=False,
        node=node,
    ).to_dict()
