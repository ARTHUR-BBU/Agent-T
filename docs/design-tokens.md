# 设计变量对照（视觉标准 v0.1）

对应 `app/static/styles.css` 中 `:root`。界面微调以本表为准，避免样式漂移。

## 色板 `--color-*`

| Token | 值 | 用途 |
|------|-----|------|
| `--color-bg` | `#F5F5F7` | 页面底 |
| `--color-surface` | `#FFFFFF` | 卡片/列表 |
| `--color-text-primary` | `#1D1D1F` | 主文案 |
| `--color-text-secondary` | `#6E6E73` | 次文案 |
| `--color-text-tertiary` | `#86868B` | 更弱提示 |
| `--color-divider` | `#D2D2D7` | 分割线 |
| `--color-accent` | `#0071E3` | 主按钮 |
| `--color-attention` | `#F5A524` | 需关注条 |
| `--color-attention-bg` | `#FFF1D6` | 需关注标签底 |
| `--highlight-bg` | `#FFE08A` | 原文关键词黄底 |
| `--highlight-underline` | `#FF9F0A` | 关键词下划线 |
| `--color-pass` / `--color-pass-bg` | `#34C759` / `#E8F8EE` | 通过 |
| `--color-danger` | `#FF3B30` | 错误 |
| `--color-attention-bar` | `#FF9F0A` | 规则「需关注」左侧 3px 条 |
| `--color-blind` | `#AF52DE` | 补盲候选左侧条 / 描边徽章 |
| `--color-blind-bg` | `#F9F0FF` | 补盲候选行底 |

## 字号 `--type-*`

| Token | 规格 |
|------|------|
| `--type-title` | 600 / 30px |
| `--type-body` | 400 / 17px |
| `--type-caption` | 400 / 13px |
| `--type-label` | 600 / 13px |

## 圆角 / 间距 / 控件

| Token | 值 |
|------|-----|
| `--radius-card` | 12px |
| `--radius-btn` / `--radius-input` | 10px |
| `--space-1`…`--space-4` | 8 / 16 / 24 / 32 |
| `--row-height` | 52px |
| `--btn-height` | 44px |

## 动效

- 过渡 ≤ **150ms**
- 禁止弹跳、闪红、长动画

## 补盲视觉（M2.5）

- 规则「需关注」：琥珀左边条 `--color-attention-bar` + 实心徽章「规则」
- 补盲候选：薰衣草左边条 `--color-blind` + 描边徽章「补盲」+ 灰字说明「候选，需人工确认」；行底 `--color-blind-bg`
- 开关关闭（`BLIND_SPOT_ENABLED` 非真）：不出现补盲样式与文案
