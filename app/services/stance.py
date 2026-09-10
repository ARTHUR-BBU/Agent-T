"""立场输入（阶段 1.3）：声明 + 防错配，绝不触碰规则引擎。

法理内核（法务老钱裁决书 2026-09-09）：合同审查的「立场」决定的是*风险读向*
（谁的损失、谁被绑住），不是*风险存在性*——存在性归尺子，读向归声明。
因此：
- 立场不进 prompt、不进 checklist 判定、不产生新档位；
- 可审立场随品类元数据（checklist YAML stances 块）下发，不可审立场
  UI 不渲染 + 前馈小字（API 层 422 兜底），**不新增任何阻断弹窗**——
  立场错配不换尺子只换读法，不构成拦截事由；
- 报告声明文案是本模块单一来源（前端与 docx 共用），必须包含
  「核查口径不因立场而改变」同义表述，否则构成误导（裁决 Q4 红线）。
"""
from __future__ import annotations

from typing import Any

from app.services.checklist import load_checklist

DEFAULT_STANCES: dict[str, Any] = {
    "view": None,
    "allowed": ["neutral"],
    "labels": {"neutral": "中性（未声明）"},
}

NEUTRAL = "neutral"

# 报告声明视角（老钱裁决书第三节定稿，出现在结果页头部与 docx 封面）。
# 措辞红线：声明立场的版本必须含「核查口径」一致表述；中性版本必须
# 说明默认阅读视角 + 方向可能不适用的提示。
_DECLARATIONS: dict[tuple[str, str], str] = {
    ("procurement", "buyer"): (
        "您声明代表买方：以下「需关注」均指对买方不利的条款。"
        "核查口径与未声明立场时完全一致，本报告为系统规则核查结果，不构成法律意见。"
    ),
    ("procurement", "neutral"): (
        "您未声明代表方：本报告默认按买方阅读视角提示风险；"
        "若您代表卖方/供货方，结论方向可能不适用，请谨慎参考。"
    ),
    ("lease", "lessee"): (
        "您声明代表承租方：以下「需关注」均指对承租方不利的条款。"
        "核查口径与未声明立场时完全一致，本报告为系统规则核查结果，不构成法律意见。"
    ),
    ("lease", "neutral"): (
        "您未声明代表方：本报告默认按承租方阅读视角提示风险；"
        "若您代表出租方，结论方向可能不适用，请谨慎参考。"
    ),
    ("nda", "disclosing"): (
        "您声明代表披露保密信息的一方（披露方）。本报告按通用风险核查，"
        "不因立场改变核查口径；方向性条款会在说明中注明偏向哪一方。"
    ),
    ("nda", "receiving"): (
        "您声明代表接收保密信息的一方（接收方）。本报告按通用风险核查，"
        "不因立场改变核查口径；方向性条款会在说明中注明偏向哪一方。"
    ),
    ("nda", "neutral"): (
        "您未声明代表方：本报告按通用风险核查，不预设立场；"
        "方向性条款（如保密义务、知识产权归属）会在说明中注明偏向哪一方。"
    ),
}

# 分支 C 知情提示（非阻断，老钱裁决书第二节；检出对方视角起草 + 中性立场时
# 出现在结果页与 docx 封面，「知情权不能省，打断权必须不给」）
STANCE_NOTICE = (
    "品类知情提示：检测到本文件以{other}立场起草，以下结论均按{view}阅读视角给出；"
    "若您实际代表{other}，本系统暂不支持该立场审查，结论方向请勿直接采信。"
)

# 预审检出对方视角起草的标记词（detected_type 内，与预审 prompt 约定一致）
_COUNTERPARTY_VIEW_MARKERS = {
    "procurement": ("卖方视角", "卖方"),
    "lease": ("出租方视角", "出租方"),
}


def get_stances(category: str) -> dict[str, Any]:
    """品类立场元数据（YAML stances 块，缺失回默认）。"""
    cfg = load_checklist(category)
    stances = cfg.get("stances")
    if not isinstance(stances, dict) or not stances.get("allowed"):
        return dict(DEFAULT_STANCES)
    return {
        "view": stances.get("view"),
        "allowed": [str(s) for s in stances.get("allowed") or ["neutral"]],
        "labels": {str(k): str(v) for k, v in (stances.get("labels") or {}).items()}
        or dict(DEFAULT_STANCES["labels"]),
    }


def is_allowed(category: str, stance: str) -> bool:
    return stance in get_stances(category)["allowed"]


def stance_label(category: str, stance: str) -> str:
    labels = get_stances(category)["labels"]
    return labels.get(stance) or stance


def view_label(category: str) -> str:
    """清单内建视角的中文标签（如 采购→买方（采购方））；NDA 无内建视角返回空。"""
    view = get_stances(category)["view"]
    return stance_label(category, view) if view else ""


def declaration(category: str, stance: str) -> str:
    """报告声明文案；未知组合回退中性通用文案（绝不返回空——声明不能缺席）。"""
    text = _DECLARATIONS.get((category, stance))
    if text:
        return text
    return _DECLARATIONS.get((category, NEUTRAL)) or (
        "本报告为系统规则核查结果，不构成法律意见。"
    )


def counterparty_view_notice(category: str) -> str:
    """分支 C 知情提示文案；该品类无对方视角标记词时返回空。"""
    marker = _COUNTERPARTY_VIEW_MARKERS.get(category)
    if not marker:
        return ""
    other = marker[1]
    view = view_label(category)
    return STANCE_NOTICE.format(other=other, view=view or "默认")


def has_counterparty_view_marker(category: str, detected_type: str) -> bool:
    """预审 detected_type 是否携带对方视角起草标记。"""
    marker = _COUNTERPARTY_VIEW_MARKERS.get(category)
    if not marker:
        return False
    return bool(detected_type) and marker[0] in detected_type
