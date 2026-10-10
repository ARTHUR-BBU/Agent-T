# 施工卡：E2E flake 修复批（终态等待，小批）

> 依据：E2E flake 家族今日频次上升（scorecard_hidden 红 4 次跨提交+本地 3/3 绿实证为 CI 环境竞态）；小智娘门禁 P3-1 立项建议
> 性质：纯测试代码改动，零生产行为变化

## 根因（A5 优化的测试盲区）

`app/static/app.js` pollReview：processing 中间态只要 items 非空即渲染清单（partial，A5「规则已出即可展示」）。测试 `_upload` 等到 `.item` visible 返回——可能是 partial 渲染：评分卡 note（done 时画）、导出按钮 href（done 时填）都还没到位。立即断言 → 竞态红。

## T1 终态等待 helper

`_wait_terminal(page)`：等 `#btn-export-report` 的 href 填充为 `/api/review/…`（仅 done 终态渲染路径填充，app.js:533），timeout 30s。语义=「等到 done 终态渲染完成」。

## T2 三个 flake 用例接入

- test_scorecard_hidden_with_no_key_note
- test_export_report_button_and_download
- test_full_review_flow_renders_results

**不改 `_upload` 本身**（其他用例可能依赖 partial 期语义，避免面外影响）。

## 放行条件

本地 E2E 全量绿（chromium 已装）+ CI 绿 + 连续观察：后续 3 次部署 CI 无本家族红。

## 边界

其余 E2E 用例不在本批（无 flake 证据不动）；pytest 重试插件不引入（掩盖真回归）。
