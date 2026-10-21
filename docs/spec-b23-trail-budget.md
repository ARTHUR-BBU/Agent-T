# B2-3 两项升级专项施工卡：留痕硬接口 + 建议层预算闸（v0.2）

> 上游：ChatGPT P1 立项（PR #119 复核：留痕八字段不全是系统性存量缺口）+ 受控试运行批复
> 的「稳定开放」前置门条款 + spec-b2 §3.1 硬接口第 4 条（留痕八字段）+ 施工卡 v0.2 注记 4（parse_failed 账本精度挂账）。
> 范围：给建议层（及全部 LLM 调用路径）补齐完整证据链 + 给建议层调用装预算闸。
> 定位：**「稳定开放」给真实用户使用前的最后一道工程门**；不改变任何审查行为。

## 0. 任务简介（大白话，给用户）

**专项一「身份证系统升级」**：现在账本（记录每次 AI 调用的登记簿）只记了半套信息——
能查到「几点打的电话、打了多久、通没通」，但缺三样关键信息：
①**身份证号（trace_id）**：没法把「同一次用户操作里的一串调用」串成一条完整证据链
（比如用户传一份合同 → 建议层看了一眼 → 预审又看了一眼 → 审查员细看——这三次调用
现在各记各的，对不上号）；
②**原始回执（raw_output）**：模型到底回了什么结构化内容，账本上没有——所以才会出现
「账本记 success 但其实没听懂」的盲区；
③**考题指纹（prompt_hash）**：发出去的提示词没留指纹，事后没法核对「当时问的是哪套话术」。
本专项把这三样补进账本，并保证旧账本数据库平滑升级不炸。

**专项二「建议层装收银台」**：正式审查早就有预算收银台（每次审查最多 18 次调用），
但新上岗的建议层还没接入——真实用户流量下没有闸。本专项给建议层装独立小额度
（默认每次上传最多 2 次调用：1 次判定 + 1 次重试），超了就静默跳过建议（用户无感，
审查照常），账本记 budget_exceeded。

**两件事都不改变审查结果**：规则引擎、评分、报告一字不动；开关关闭+预算关闭时行为与现状完全一致。

## 1. 专项一：留痕硬接口升级

### 1.1 交付物

1. **表结构扩展**（llm_calls 三新列，幂等迁移）：
   - `trace_id TEXT`——同一次用户操作的调用关联键（uuid4 hex 16，与 call_id 同规格）
   - `raw_output TEXT`——模型的原始结构化输出（仅 decision 形态节点；**脱敏红线**：
     只存模型返回的判断 JSON，不含合同文本；超 2048 字符截断+截断标记）
   - `prompt_hash TEXT`——实际发送提示词（system+user 全文）的 SHA256 前 16 位
     （哈希不含内容原文，无泄露面）
   - 迁移：`PRAGMA table_info` 检缺列再 `ALTER TABLE ADD COLUMN`（旧库启动零破坏，
     旧行三列为 NULL 合法）；`_ensure_table` 的 CREATE 同步加列
2. **trace_id 生命周期**：
   - `upload` / `ask` 路由入口各生成一个，contextvar 注入；
   - **跨线程传递（关键难点）**：审查管线跑在 daemon 线程（contextvar 不自动继承）——
     trace_id 放进 pipeline state 显式传递，`record_node` 增加 `trace_id` 显式参数
     （缺省回读 contextvar）；`run_in_threadpool` 路径用 `contextvars.copy_context()` 包装
   - `LLMCallRecord` 增 `trace_id` 字段，transport 单点自动落账
3. **建议层 review_id 回填**（补 P0-D1 注记）：审查单创建后
   `UPDATE llm_calls SET review_id=? WHERE trace_id=? AND review_id IS NULL`（单语句，回填失败只 warning）
4. **解析失败细分**：`decision_judgment` 的 JSONDecodeError 从 `provider_error` 改记
   `parse_failed`（对齐 outcome 枚举既有值；重试白名单不变——解析失败属确定性失败不重试）；
   同步修施工卡 v0.2 注记 4 挂账
5. **可查询**：`query()` 增 `trace_id` 过滤参数
6. **trace_id 单点生成（用户批注②）**：仅在 `upload`/`ask` 路由入口生成一次，
   贯穿全程；**各 AI 模块禁止自生成**（record_node 只接受透传，不提供生成逻辑）——
   防止「身份证裂成多张」；测试断言全链调用同号（§3.1）
7. **raw_output 三重约束（用户批注①）**：
   - **脱敏**：模型结构化输出可能复述合同原文——落账前必须过截断（≤2048 字符+
     截断标记），并断言不含连续 ≥50 字符的合同原文片段（测试 §3.7）
   - **保存期限**：随 llm_calls 同一 TTL 域（store TTL 到期一并清理），不单独立册
   - **查询权限**：与账本同权同限（代码层 query()，无独立 HTTP 面），不新增暴露面

### 1.2 八字段对齐声明（spec-b2 §3.1 第 4 条）

| 字段 | 现状 | 本专项 |
|---|---|---|
| 供应商/模型版本 | ✅ provider/model | 不动 |
| 提示词哈希 | prompt_version（版本号非哈希） | ✚ prompt_hash |
| 输入覆盖范围 | chars_sent/truncated/document_version | 不动 |
| 延迟 | ✅ latency_ms | 不动 |
| 费用 | ✅ usage | 不动 |
| 原始结构化输出 | ❌ 无（挂账根源） | ✚ raw_output |
| 降级原因 | outcome+error_detail（解析失败错记 provider_error） | parse_failed 细分 |
| 关联键 | review_id（建议层缺失） | ✚ trace_id+回填 |

### 1.3 非目标

- 不做 HTTP 管理端点（query() 仅供代码层/排障脚本；管理界面另议）
- 不回填历史账本行（旧行 NULL 合法，TTL 自然淘汰）
- 不动 prompt_version 语义与 PROMPT_VERSION 常量
- ask 路由仍不接预算（既有挂账不变）

## 2. 专项二：建议层预算闸

### 2.1 交付物

1. **独立额度**：`LLM_SUGGEST_BUDGET_PER_UPLOAD`（默认 2=判定 1+重试 1；0/负=不限；
   垃圾值回退默认同 `limit_from_env` 口径）——**不动主预算 DEFAULT_BUDGET=18**：
   建议层有独立小闸可单独熔断，且不挤压既有链路「异议层排预算末位」的优先级语义
2. **接线点**：upload 路由建议层入口 `try_consume(1)`；瞬态重试前再 `try_consume(1)`；
   拒绝→`SuggestOutcome(skip_reason="budget_exceeded")` 静默跳过（**建议层是增强不是
   主链**——降级绝不阻断上传），账本 outcome=`budget_exceeded`
   （枚举沿用既有 `budget_exceeded`，与主预算同款口径；用户批注③的
   budget_exhausted 即此值——**对用户静默、对账本必须留痕**，且记入
   SuggestOutcome.skip_reason 供调用侧观测，不允许「悄悄失效无痕迹」）
3. **`0=不限` 仅限开发/受控试运行（用户批注④）**：生产环境把它当无限额度是后门——
   非试运行模式下 `LLM_SUGGEST_BUDGET_PER_UPLOAD<=0` 触发**启动失败**（对齐限频
   变量半配置拒绝先例）；确需不限必须显式设危险开关
   `LLM_ALLOW_UNLIMITED_SUGGEST=true`（启动日志 warning 留审计痕迹）。
   开发/受控试运行（DEBUG 或试运行标记环境）不受限
4. **主预算关闭态直通不变**（budget 语义与 `new_review_budget` 一致）

### 2.2 非目标

- 不做全局日限额/跨请求计数器（需持久化注册表与清理钩子，另批）
- 不动 /api/ask 限频现状

## 3. 测试要求

1. **五路径可查询（ChatGPT P1 验收核心）**：预审/评分（model_review）/质量/异议/追问
   五条既有路径+建议层，各路径集成测试断言——账本行带 trace_id、按 trace_id 可聚合
   查出该次操作全链调用
2. 跨线程传递：daemon 线程内 pipeline 节点的账本行带正确 trace_id（防 contextvar 断链）
3. **迁移三步真实验收（用户批注⑤，不做假绿）**：构造旧 schema 库 fixture
   （无三列+若干历史行）→①启动迁移（自动加列）→②读历史记录（旧行三列 NULL
   可查不炸）→③写入新记录（三列完整落账）——三步全过才算平滑升级成立
4. 建议层预算：额度内正常/额度耗尽零外部调用断言（无网络请求）+账本 budget_exceeded
   +skip_reason 落痕/重试前二次扣减/关闭态直通/**生产模式 0=不限 启动拒绝+
   危险开关审计痕迹**
4. 建议层预算：额度内正常/额度耗尽零外部调用断言（无网络请求）+账本 budget_exceeded
   +skip_reason 落痕/重试前二次扣减/关闭态直通
5. parse_failed：模型回 200+畸形 JSON→账本 outcome=parse_failed（重试白名单行为不变）
6. 行为零变化守卫：开关关+预算关时，上传响应与现状一致（既有全量回归为据）
7. raw_output 脱敏断言：不含 ≥50 字符连续合同原文片段、超长截断有标记
8. trace_id 全链同号断言：一次上传的 suggest+precheck+审查管线（daemon 线程）+
   ask 各路径账本行 trace_id 一致且非空；模块自生成路径不存在（代码层无生成入口）

## 4. 验收门

1. ruff+pyright（0 错误基线）+全量测试绿
2. 新增测试全绿且覆盖 §3 清单
3. 生产部署后 fail-loud 核验：建议层账本行三新列非空（trace_id/prompt_hash 必填，
   raw_output decision 节点必填）
4. 本施工卡用户复批 + ChatGPT 审计通过后开工

## 5. 审计问题清单（用户批注 2026-10-21，转外审逐条复核）

1. raw_output 防「换个字段重存合同」：脱敏+截断上限+保存期限（随 TTL）+查询权限
   （与账本同权）四项约束是否闭环？（§1.1.7）
2. trace_id 起点锁死：仅 upload/ask 入口单点生成、模块禁止自生成——设计是否防住
   「身份证裂成多张」？（§1.1.6）
3. 预算耗尽不得只静默：账本 budget_exceeded+skip_reason 双落痕，对用户不打扰对
   内部可观测——口径是否足够？（§2.1.2）
4. `0=不限` 后门封堵：生产启动拒绝+显式危险开关+审计痕迹——门槛是否到位？
   （§2.1.3）
5. 迁移验收真实化：旧库 fixture 三步验收（升级/读历史/写新记录）替代口头承诺。
   （§3.3）

## 6. 风险与回滚

- 迁移风险：ALTER 失败→账本 emit 全程吞异常（既有纪律），审查主链不受影响；
  部署 SOP 先备份账本（B2-3 事故教训已入册）
- 行为风险：预算闸误拦→开关式回退（额度设 0=不限，env 一改即回退，无需回滚代码）
- 回滚路径：三新列为增量，代码回退后旧代码读新库不炸（列多余无害）
