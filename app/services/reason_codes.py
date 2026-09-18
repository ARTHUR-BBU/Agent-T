"""标准 reason code（宪法 P0-D3：全项目 LLM 层失败/降级码的唯一权威定义）。

背景（LLM 规范 20 节 + 宪法一致性审计 D3）：reason 码此前散落为约 14 种
字符串字面量（含编外 map_fail_fallback、recheck_budget_exceeded），无共享
枚举——拼错无编译期拦截，消费方（前端/文档/监控）无法穷举。

用法：
- 产出层（quality/objection/precheck/scorecard/verify/model_review）写
  reason 时一律 `Reason.X.value`；
- 消费层（前端/文档）以本文件为唯一字典；
- 新增码必须先在这里登记（含语义与产出层），再使用。

未收敛说明：verify.recheck_budget_exceeded 当前是 RuntimeError 信号码
（进程内控制流），PrecheckOutcome.skip_reason=busy 是分诊语义——均登记
在 EXTENDED_CODES 供盘点，暂不并入 Reason（避免跨层语义混淆）。
"""
from __future__ import annotations

from enum import Enum


class Reason(str, Enum):
    """LLM 层标准 reason code（各层 info.reason 字段的合法值域）。"""

    DISABLED = "disabled"                # 功能开关关闭（ENABLED=false）
    NO_LLM_KEY = "no_llm_key"            # 未配置模型 Key（规则引擎不受影响）
    LLM_ERROR = "llm_error"              # 供应商调用异常（超时/网络/5xx）
    PARSE_FAILED = "parse_failed"        # 模型输出解析失败/禁语重试后仍脏
    BUDGET_EXCEEDED = "budget_exceeded"  # 单次审查预算耗尽（闸门拒绝）
    ERROR = "error"                      # 本层内部异常兜底
    NOT_ATTEMPTED = "not_attempted"      # 前置条件未满足，尚未尝试（如 verify 无数据）
    INCOMPLETE_MODEL_OUTPUT = "incomplete_model_output"  # 模型输出不完整，F03 语义降级
    MAP_FAIL_FALLBACK = "map_fail_fallback"  # 长合同 map 失败回退头尾采样


# 兼容别名：历史代码直接引用字符串值
DISABLED = Reason.DISABLED.value
NO_LLM_KEY = Reason.NO_LLM_KEY.value
LLM_ERROR = Reason.LLM_ERROR.value
PARSE_FAILED = Reason.PARSE_FAILED.value
BUDGET_EXCEEDED = Reason.BUDGET_EXCEEDED.value
ERROR = Reason.ERROR.value
NOT_ATTEMPTED = Reason.NOT_ATTEMPTED.value
INCOMPLETE_MODEL_OUTPUT = Reason.INCOMPLETE_MODEL_OUTPUT.value
MAP_FAIL_FALLBACK = Reason.MAP_FAIL_FALLBACK.value

# 全部合法值（消费方穷举/校验用）
ALL = frozenset(r.value for r in Reason)

# 编外码（非 info.reason 语义，登记备查——见模块 docstring）
EXTENDED_CODES = frozenset({
    "budget_exhausted",        # map 循环中止原因（stop_reason），区别于闸门拒绝
    "recheck_budget_exceeded", # verify 再核次数信号（RuntimeError 控制流）
    "busy",                    # precheck 分诊忙（skip_reason）
})
