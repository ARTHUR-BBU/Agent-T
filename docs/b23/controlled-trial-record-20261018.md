# B2-3 分诊台建议层受控试运行记录（2026-10-18）

> 批复：ChatGPT 受控试运行规程（用户转达批准）。
> 结论先行：**触发熔断条款第 4 条（供应商持续故障），开关已关，试运行中止。**

## 1. 部署记录

| 项 | 值 |
|---|---|
| 部署版本 | agent-t:f4a1252（PR #119 merge commit，HEAD=8d8a2f2 复核后合并） |
| 合并前复核 | HEAD=8d8a2f2、CI lint 29s+pytest 2m54s 双绿、CLEAN ✅ |
| 部署方式 | git archive→scp→docker build -f Dockerfile.server→换容器（env 复用） |
| 开关状态（部署时） | CATEGORY_SUGGEST_ENABLED=true + TYPESAFE_API_KEY（ssh 管道注入，未落命令行） |
| 安全门 | health=200 / root=401 / api=401 ✅（部署时与熔断后各一轮） |

## 2. 已知风险如实保留（批复原文三条，部署时在案）

1. 建议层完整八字段证据链与审查单关联尚未补齐；
2. 分类调用尚未接入硬预算闸；
3. 校准主要来自公开示范文本，中低置信真实合同覆盖不足。

## 3. 试运行实测（Jev 主通道）

账本（llm_calls，node=category_suggest）三条调用记录：

| chars_sent | truncated | latency_ms | outcome | error_detail |
|---|---|---|---|---|
| 610 | 0 | 966 | provider_error | 451 "Typesafe is not available in your region." |
| 8057 | 1 | 793 | provider_error | 同上 |
| 8057 | 1 | 740 | provider_error | 同上 |

**根因：HTTP 451 地域封锁（确定性拒绝，非偶发）**——生产服务器（阿里云中国区）出网调 api.typesafe.ai 被 TypeSafe 按地域拒绝；本地开发机出口不同故校准/试射全程可达。

## 4. 六项验收实测结果（弹窗均由 precheck 层兜底产生——建议层因 451 静默降级，设计行为）

| 场景 | 实测 |
|---|---|
| 1 建议一致→正常审查 | ⚠️ N/A（建议层未工作，uploaded 由降级路径产生） |
| 2 高置信不一致→confirm | ⚠️ precheck 兜底弹窗（非建议层）；suggested=None |
| 3 低置信不一致→confirm 改文案 | ⚠️ 同上，未覆盖 |
| 4 force 确认重传→正常审查 | ✅ uploaded+review_id（bf1d436a7330） |
| 5 不支持品类建议→「暂不在支持范围」 | ⚠️ precheck 兜底；detected=技术咨询合同 |
| 6 与开关关对照规则状态不变 | ✅ 本地全量 1161 passed 覆盖（CI 为据） |

## 5. 熔断执行（批复预案第 4 条）与一次操作事故

- **熔断**：开关移除（env 重建容器），安全门复跑 200/401/401 ✅，镜像 agent-t:f4a1252 保留（TYPE SAFE key 保留在 env 便于恢复）。
- **操作事故（如实记录）**：熔断 `docker rm -f` 前未备份容器内 `/tmp/agent-t-reviews.db`——账本三条 451 原始行随旧容器销毁。补救：三条记录的 error_detail/chars_sent/latency 在熔断前已完整抄录至本文档 §3（上表即手抄固化），**原始数据库行不可恢复**。教训已实际写入部署文档 SOP（docs/deploy-server.md 日常更新流程新增「换容器前必先备份账本」步骤）。

## 6. 后续（待用户与 ChatGPT 侧再定）

- Jev 地域封锁为供应商硬限制，中国区生产服务器**不可用**（除非出口代理，不建议）。
- 备选：建议层主通道回退官方智谱 glm-4-flash（国内直连免费；代码为 GLM→Jev 切换前形态，恢复成本低）；或维持开关关闭、建议层缓发。
- 两项升级专项（留痕 trace_id+持久化、预算闸）不因试运行中止而取消，仍为「稳定开放」前置门。
