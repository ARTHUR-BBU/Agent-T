# B2-3 两项升级专项施工卡：留痕硬接口 + 建议层预算闸（v0.3）

> 上游：ChatGPT P1 立项（PR #119 复核）+「稳定开放」前置门条款 + spec-b2 §3.1 硬接口
> 第 4 条 + 施工卡 v0.2 注记 4。v0.2 经用户批注五条 + ChatGPT 设计审计（2026-10-21，
> 5×P1+隐私验收线）后重写为本版——**v0.3 处置表见 §6，放行条件见 §7**。
> 范围：给建议层（及全部 LLM 调用路径）补齐完整证据链 + 给建议层调用装预算闸。
> 定位：**「稳定开放」给真实用户使用前的最后一道工程门**；不改变任何审查行为。

## 0. 任务简介（大白话，给用户）

**专项一「身份证系统升级」**：给每次用户操作发一张全程有效的身份证（trace_id），
把建议层、预审、审查员、追问的一串 AI 调用串成一条完整证据链；账本补记三样缺的
东西——身份证号、模型原始回执（脱敏后的结构化部分）、考题指纹。连「用户在确认
弹窗点了确认再重传」这种情况也专门处理（续办令牌），保证弹窗前后仍是同一张证。

**专项二「建议层装收银台」**：建议层独立小额度（默认每次上传 2 次调用），超了
静默跳过、审查照常——但**账本必须记下这次拦截**（不能光在心里记，得落在账上）。
生产环境禁止把额度改成无限（那是后门），正常回退手段是关建议层开关，不是开无限。

**两件事都不改变审查结果**：规则引擎、评分、报告一字不动；开关关闭+预算关闭时行为
与现状完全一致。

## 1. 专项一：留痕硬接口升级

### 1.1 账本写入改造（P1-1：列位迁移，杜绝「按旧房间号送家具」）

现状 `emit()` 用无列名 `INSERT INTO llm_calls VALUES (...)`——旧库 ALTER 加列后
新列追加表尾、新库 CREATE 列序又可能与 to_row 顺序不一致，按位置写入会把
trace_id/raw_output 写进错误字段。改造：

1. **全量显式列名写入**：`INSERT OR REPLACE INTO llm_calls
   (call_id, review_id, node, provider, model, prompt_version, document_version,
   chars_sent, truncated, attempt, started_at, latency_ms, outcome, usage,
   gate_counts, error_detail, trace_id, raw_output, prompt_hash, created_at)
   VALUES (...)`——`emit()`/`emit_skip()`/`query()` 一律按列名，位置写入清零
2. **迁移三步真实验收**（用户批注⑤）：测试构造**真实旧 schema** 库 fixture
   （旧 CREATE 语句+若干历史行）→①启动迁移→②逐列名读回历史行核对→③写新行
   再逐列名读回核对——三步全过才算平滑升级成立
3. **并发首启迁移守卫**：迁移包在 `BEGIN IMMEDIATE` 事务+模块级锁内；
   `duplicate column name` 捕获为幂等良性（两线程同判缺列不撞车）

### 1.2 新增三列

- `trace_id TEXT`——同一次用户操作的调用关联键（uuid4 hex 16）
- `raw_output TEXT`——**字段白名单结构化回执**（见 §1.5，不是任意原文回包）
- `prompt_hash TEXT`——实际发送提示词全文的 SHA256 前 16 位（哈希无泄露面）

迁移：`PRAGMA table_info` 检缺列再 `ALTER TABLE ADD COLUMN`（旧行三列 NULL 合法）；
`_ensure_table` 的 CREATE 同步加列。

### 1.3 trace_id 生命周期（P1-2：确认弹窗续办链路）

1. **单点生成**：仅在 `upload`/`ask` 路由入口生成一次，贯穿全程；各 AI 模块
   **禁止自生成**（record_node 只透传，不提供生成逻辑）
2. **跨线程传递**：审查管线跑在 daemon 线程（contextvar 不自动继承）——trace_id
   进 pipeline state 显式传递，`record_node` 增 `trace_id` 显式参数（缺省回读
   contextvar）；`run_in_threadpool` 路径用 `contextvars.copy_context()` 包装
3. **续办令牌（本专项核心承诺的补全）**：category_confirm 在 review 创建前返回，
   用户确认后 force 重传是新 HTTP 请求——若每次 upload 都发新 trace_id，建议层
   调用与最终审查就裂成两张证，回填失效。设计：
   - 建议 `category_confirm` 响应附 `continuation_token`：服务端 HMAC-SHA256 签发，
     绑定三元组 `(trace_id, 文件 SHA256, 过期时间 15 分钟)`，密钥进程内随机生成
     （重启失效=回退为「重新上传产生新证」，可接受降级）
   - force 重传可携带该 token（可选表单字段）：服务端验签+文件哈希匹配+未过期，
     通过则**恢复原 trace_id**——建议层调用与最终审查串回一条链
   - **安全边界**：前端不能提交任意 trace_id（无签名即拒绝，静默按新证处理）——
     防止把不相干合同的账串在一起；token 绑文件哈希，换文件重传同样不继承
   - 前端配合（阳仔，最小改动）：category_confirm 响应里的 token 透传到 force
     重传表单。不带 token 行为不变（链路断开但不报错，测试锁定两态）
4. **建议层 review_id 回填**：审查单创建后
   `UPDATE llm_calls SET review_id=? WHERE trace_id=? AND review_id IS NULL`
   （单语句；失败只 warning）

### 1.4 解析后账本结算（P1-3：先创建、后结算）

现状 decision_judgment 在 transport 层响应后立即 emit，而业务解析在建议层服务里——
transport 无从判定业务成败，白名单解析失败时账本已定格 success。改造为两段式：

1. **transport 段**：`decision_judgment` 完成 HTTP 调用后 emit 账本行并返回
   `call_id`+原始响应体。outcome 只判 transport 层事实：
   - `provider_error`：HTTP 非 200，或 200 但响应体非合法 JSON（供应商协议/格式坏）
   - `timeout`：超时（既有）
   - 此时落 `success` 的是**「transport 层成功」临时态**，非终态
2. **结算段**：解析层 `_parse_verdict` 后调用新增接口
   `settle(call_id, outcome, raw_output, error_detail)`——按 call_id 原子 UPDATE，
   仅允许 `success→{success, parse_failed}` 迁移（provider_error/timeout 不可结算改写）；
   解析路径 finally 保证必结算（进程在结算前崩溃的残留=行无 raw_output，可检测，
   接受为已知残余并记录）
3. **三分法定义**（全链统一口径）：
   - `provider_error`：HTTP 或供应商响应格式坏
   - `parse_failed`：拿到模型业务输出（合法 JSON）但白名单品类/字段结构不合格
   - `success`：解析并通过结构校验
4. 范围：本卡只改造 `decision_judgment`（建议层路径）；`chat_completion` 存量节点
   的同款缺口**维持挂账**（施工卡 v0.2 注记 4 的「统一改造」另批），本卡完成后
   该挂账的解法范本即此节
5. 同步撤销 v0.2「JSONDecodeError 改记 parse_failed」条目——JSONDecodeError 归
   provider_error（协议层），见上三分法

### 1.5 raw_output 隐私验收线（用户批注①+ChatGPT 隐私条款）

「不含合同原文」不靠承诺成立，靠结构保证：

1. **字段白名单**：只存解析后的结构化回执允许字段（如
   `{category, confidence, probabilities, answers}` 白名单枚举），**不存任意原文回包**
2. **解析失败**：不存原始响应体——存受控错误摘要（如「unrecognized category: X」
   「schema mismatch: field Y」）+ 原始响应体的 SHA256 前 16 位指纹（可对账不可复原）
3. **截断**：白名单序列化后仍超 2048 字符→截断+截断标记
4. **保存期限**：随 llm_calls 同一 TTL 域（store TTL 到期一并清理），不单独立册
5. **查询权限**：与账本同权同限（代码层 query()，无独立 HTTP 面）
6. **对抗测试**：fixture 模型输出**故意复述合同句子**（藏进 probability 字段名/
   自由文本等位置）→断言落账 raw_output 不含该片段（白名单结构天然拦截）

### 1.6 可查询与全链同号验收

1. `query()` 增 `trace_id` 过滤参数
2. **五路径+建议层可查询**（ChatGPT P1 验收核心）：预审/评分（model_review）/
   质量/异议/追问五条既有路径+建议层，各路径集成测试断言——账本行带 trace_id、
   按 trace_id 可聚合查出该次操作全链调用
3. 全链同号断言：一次上传的 suggest+precheck+审查管线（daemon 线程）+续办重传+
   ask 各路径账本行 trace_id 一致且非空；模块自生成路径不存在（代码层无生成入口）

## 2. 专项二：建议层预算闸

### 2.1 交付物

1. **独立额度**：`LLM_SUGGEST_BUDGET_PER_UPLOAD`（默认 2=判定 1+重试 1；
   垃圾值回退默认同 `limit_from_env` 口径）——**不动主预算 DEFAULT_BUDGET=18**：
   建议层独立小闸可单独熔断，不挤压既有链路「异议层排预算末位」的优先级语义
2. **接线点**：upload 路由建议层入口 `try_consume(1)`；瞬态重试前再 `try_consume(1)`；
   拒绝→`SuggestOutcome(skip_reason="budget_exceeded")` 静默跳过（建议层是增强不是
   主链，降级绝不阻断上传）。
3. **拦截必须落账（P1-4：emit_skip）**：预算拦住=零网络请求=不进 transport=既有
   emit 路径根本不会触发——故新增受控账本事件接口 `emit_skip(node, provider, model,
   trace_id, outcome="budget_exceeded", error_detail=含额度信息)`，为「未发出的调用」
   落一条显式账（call_id 照发、chars_sent/latency 为空）。**否则「收银台拦住了」
   只是内存里的私账，对账时查无此事**。测试断言 SQLite 真有这一行
4. **`0/负=不限` 封堵（P1-5+用户批注④）**：生产环境把它当无限额度是后门——
   非试运行模式下 `<=0` 触发**启动失败**（对齐限频变量半配置拒绝先例）；确需不限
   须显式危险开关 `LLM_ALLOW_UNLIMITED_SUGGEST=true`（启动 warning 留审计痕迹）。
   开发/受控试运行不受限
5. **正常回退路径=关闭建议层**（`CATEGORY_SUGGEST_ENABLED=false`），**不是**把额度
   改成无限——「费用失控时恰好最不该开无限」；主预算关闭态直通语义不变

### 2.2 非目标

- 不做全局日限额/跨请求计数器（需持久化注册表与清理钩子，另批）
- 不动 /api/ask 限频现状；ask 路由仍不接预算（既有挂账不变）

## 3. 测试要求（放行条件逐条对应）

1. **旧库列位迁移三步**：真实旧 schema fixture→升级→逐列名读回历史行→写新行
   逐列名读回（§1.1.2）；并发双线程首启迁移不撞车（§1.1.3）
2. **续办链路**：category_confirm 带 token→force 重传恢复原 trace_id→建议层与
   审查行同号；篡改 token/换文件/过期 token→拒绝且按新证处理不报错（§1.3.3）
3. **解析后结算**：合法 JSON 白名单外→账本终态 parse_failed（非 success）；
   非 JSON 响应→provider_error；结算接口禁止改写 provider_error/timeout；
   解析路径异常仍必结算（§1.4）
4. **预算拦截账本事件**：额度耗尽→零网络请求断言+SQLite 存在
   outcome=budget_exceeded 行（emit_skip 实证）+重试前二次扣减+关闭态直通+
   生产模式 `<=0` 启动拒绝+危险开关审计痕迹（§2.1）
5. **隐私对抗**：模型输出故意复述合同句子→落账 raw_output 无该片段；
   解析失败只留受控摘要+指纹（§1.5.6）
6. **五路径+建议层全链同号**（§1.6）
7. **行为零变化守卫**：开关关+预算关时上传响应与现状一致（既有全量回归为据）

## 4. 验收门

1. ruff+pyright（0 错误基线）+全量测试绿
2. §3 测试清单全绿
3. 生产部署后 fail-loud 核验：建议层账本行 trace_id/prompt_hash 必填非空、
   settle 后 raw_output 非空（decision 节点）
4. ChatGPT 复批本 v0.3（放行条件 §7）+用户确认后开工

## 5. 风险与回滚

- 迁移风险：ALTER 失败→账本 emit 全程吞异常（既有纪律），审查主链不受影响；
  部署 SOP 先备份账本（B2-3 事故教训已入册）
- 行为风险：预算误拦→正常回退=关建议层开关（§2.1.5），回退不碰额度语义
- 回滚路径：三新列为增量，代码回退后旧代码按显式列名读新库不炸（列多余无害）
- 续办令牌风险：进程重启 token 失效→用户重新上传产生新证（可接受降级，无数据损坏）

## 6. v0.3 处置表（ChatGPT 设计审计 2026-10-21，5×P1+隐私线逐条落位）

| 审计点 | 落位 |
|---|---|
| P1-1 列位迁移 | §1.1：显式列名写入全量化+真实旧 schema 三步验收+并发首启事务/锁守卫 |
| P1-2 确认弹窗裂证 | §1.3.3：HMAC 续办令牌（绑 trace_id+文件哈希+15min 有效期），验签恢复原证；前端透传（阳仔最小改动） |
| P1-3 落账时机 | §1.4：先创建后结算两段式（transport 返 call_id→解析层 settle 原子改写）+三分法定义+chat_completion 存量挂账不变 |
| P1-4 拦截不落账 | §2.1.3：emit_skip 受控账本事件接口，未发出的调用也留显式账 |
| P1-5 无限额度后门 | §2.1.4/2.1.5：生产启动拒绝+显式危险开关+审计痕迹；正常回退=关建议层非开无限 |
| 隐私验收线 | §1.5：字段白名单结构化回执+解析失败受控摘要/指纹+截断/TTL/同权+复述对抗测试 |

## 7. 放行条件

1. ChatGPT 复核 v0.3 确认五项 P1+隐私线处置到位（设计层复批）
2. 用户确认合并本施工卡
3. 开工后 §3 测试清单为门禁验收基线
