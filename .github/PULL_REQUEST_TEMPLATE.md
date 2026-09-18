# PR 说明

## 概要

<!-- 本次改了什么，为什么 -->

## 宪法自答（Agent-T Constitution 第十五章——影响审查逻辑的 PR 必答）

### A. 角色
- 本次修改属于：调查 / 检察 / 审判 / 律师 / 立法 / 书记 / 执行 / 监督（必选其一或多选）
- 有没有让某个角色获得新权力？<!-- 有 → 必须勾选 Constitution impact -->

### B. 证据
- [ ] 新结论能回到 exact quote / clause / span
- [ ] 证据真实且相关（非「真话凑数」）
- [ ] 不存在引用模型未见过文本的路径
- [ ] coverage 诚实记录（截断/部分读取已明示）

### C. 裁决
- 正式结果字段仍是规则引擎唯一产出：`items[].status`
- [ ] AI / 参考层不可能直接或间接覆盖它（含共享引用突变）
- [ ] 同输入可稳定重现

### D. 程序
- [ ] Graph 顺序未变，或变化已说明
- [ ] completion / unavailable / partial 语义仍准确
- [ ] 失败未被静默吞掉

### E. 测试
- [ ] 新行为有回归测试（含反例，不只 happy path）
- [ ] 覆盖：重复 / 截断 / 错误 scope / 恶意模型输出（如适用）

### F. Review
- [ ] P1/P2 全部修复，或已写 Accepted Risk（理由+影响+责任人）
- [ ] 无 unresolved review thread
- [ ] CI 绿是真实执行（非 skip / feature flag 假绿）

## Constitution impact

<!-- 以下任一为真时必须填「有」并展开说明，否则填「无」：
AI 获得直接修改 Rule Result 的能力 / 改变 Evidence 事实来源定义 /
取消人工确认边界 / 改变 Rule/AI/Human 优先级 / **改变**审计可追溯性
（含增强——schema/溯源模型任何变化）/ 允许运行时模型修改规则 /
取消关键 CI/Review 门禁 / 自动执行真实外部动作 -->

- [ ] 无
- [ ] 有（说明：）

## 回归证据

<!-- 本地测试命令与结果（附数字）、CI run 链接、部署/验收情况 -->
