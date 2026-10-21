# B2-3 两项升级专项施工卡：留痕硬接口 + 建议层预算闸（v0.5）

> 上游：ChatGPT P1 立项 +「稳定开放」前置门 + spec-b2 §3.1 硬接口第 4 条。
> 版本沿革：v0.1 初稿 → v0.2 用户批注五条 → v0.3 ChatGPT 设计审计 5×P1 全量处置
> → **v0.4 审计收敛**（「小而硬的 MVP 加固批」）→ **v0.5 终审双 P1 收口**（2026-10-21）：
①审查单持久化 trace_id 锚点+回填（review_id→trace 反向可查，正常上传闭环）
②回滚预案改为「向前兼容、不可破坏性回滚」（删列/恢复旧库只留灾难恢复场景）。
> 范围：给现有建议层补可查证据链 + 每次上传两次调用的闸。**不膨胀成全系统审计平台。**
> 两件事都不改变审查结果：开关关闭+预算关闭时行为与现状完全一致。

## 0. 任务简介（大白话，给用户）

**专项一「账本补课」**：登记簿补三样——身份证号（trace_id，串起一次上传的全部
AI 调用）、模型结构化回执（脱敏截断）、考题指纹；旧账本数据库平滑升级有真实验收。
**专项二「建议层装闸」**：每次上传最多 2 次建议层调用（判定 1+重试 1），超了静默
跳过、审查照常——但拦截必须**落在账本上**（不能只记在心里）。

## 1. 开工前三件硬事（ChatGPT 收敛后的必做项）

### 1.1 硬事一：旧账本迁移不能错列或丢记录

现状 `emit()` 用无列名 `INSERT INTO llm_calls VALUES (...)`——加列后按位置写入会把
新字段写错列；且回退旧代码时 17 个位置参数对 20 列会**每条插入静默失败**
（emit 吞异常=账本无声停摆）。改造：

1. **全量显式列名写入**：`INSERT OR REPLACE INTO llm_calls (call_id, ..., error_detail,
   trace_id, raw_output, prompt_hash, created_at) VALUES (...)`——emit/emit_skip/query
   一律按列名
2. **迁移真实验收**：测试构造真实旧 schema fixture（旧 CREATE+历史行）→启动迁移
   （PRAGMA 检缺列再 ALTER，`duplicate column name` 幂等捕获）→按列名读回历史行→
   写新行再按列名读回——三步全过
3. **回滚预案=向前兼容、不可破坏性回滚**（终审 P1-2：删列/恢复旧库=用数据损失换
   服务恢复，不是安全回滚）：
   - 本 schema 扩展定性为**只增不删、向前兼容**；新版写入统一显式列名
   - 若必须回退应用：回退目标必须是**同样使用显式列名的兼容版本**（或前向热修），
     **不允许**以 DROP COLUMN 或恢复旧备份库作为常规回滚手段——删列丢新留痕、
     恢复旧库丢部署期间新审查与账本
   - 「恢复部署前备份库 / DROP COLUMN」仅保留给**灾难恢复场景**（库损坏/数据
     污染），部署文档同步改写并注明二者区别

### 1.2 硬事二：账本区分「请求成功」与「解析成功」

现状 decision_judgment 在 transport 层响应后立即 emit success，而业务解析
（_parse_verdict 白名单校验）在建议层服务里——200+合法 JSON 但品类缺/畸形/白名单外
时账本定格 success 假绿。改造为**先创建、后结算**两段式：

1. transport 段：emit 账本行并返回 `call_id`；transport 层事实照旧判定
   （HTTP 非 200/响应非合法 JSON → `provider_error`；超时 → `timeout`）
2. 结算段：解析层新增 `settle(call_id, outcome, raw_output, error_detail)`——按
   call_id 原子 UPDATE，仅允许 `success→{success, parse_failed}` 迁移；解析路径
   finally 保证必结算
3. 三分法：`provider_error`=HTTP/协议层坏；`parse_failed`=拿到业务输出但白名单/
   结构不合格；`success`=解析且结构校验通过
4. 范围：只改造 `decision_judgment`（建议层路径）；chat_completion 存量同款缺口
   维持挂账（本节即解法范本），撤销 v0.2「JSONDecodeError 记 parse_failed」条目

### 1.3 硬事三：预算拦截必须留痕

预算拦住=零网络请求=不进 transport=既有 emit 根本不触发。新增受控账本事件接口
`emit_skip(node, provider, model, trace_id, outcome="budget_exceeded",
error_detail=含额度信息)`——为「未发出的调用」落显式账（call_id 照发）。
**否则「收银台拦住了」只是内存私账，对账查无此事。**

配套：
- 独立额度 `LLM_SUGGEST_BUDGET_PER_UPLOAD`（默认 2；不动主预算 18 的优先级语义）；
  接线：建议层入口 try_consume(1)，瞬态重试前再 try_consume(1)；拒绝→
  SuggestOutcome(skip_reason="budget_exceeded") 静默跳过（建议层是增强不是主链）+emit_skip
- **`0/负=不限` 仅限开发与紧急回退，文档写明生产配置必须为正数**；生产强制拒绝+
  危险开关审计→挂账至正式开放配置批（§6）
- 正常回退路径=关闭建议层开关，不是把额度改无限

## 2. 最小实现的留痕设计

### 2.1 trace_id（范围收敛，§6-①）

- 单点生成：upload/ask 路由入口生成一次；各 AI 模块禁自生成（record_node 只透传）
- 跨线程：审查管线 daemon 线程经 pipeline state 显式传递；record_node 增显式参数
  （缺省回读 contextvar）；run_in_threadpool 用 copy_context() 包装
- 覆盖面：一次 HTTP 上传（建议层+预审+审查管线）与 /ask
- **审查单锚点（终审 P1-1，正常上传闭环）**：审查单创建时持久化 trace_id——
  `store.create(trace_id=...)`（reviews 表 data JSON 字段，零 schema 迁移）；
  创建后将本次 trace 下尚未关联的调用行回填该 review_id（单语句
  `UPDATE llm_calls SET review_id=? WHERE trace_id=? AND review_id IS NULL`，
  失败只 warning）。半年后拿 review_id 反查「这份审查前发生过什么」：
  review_id→trace_id→建议层/预审/后台审查全链记录，一一可查
- **确认弹窗续办（category_confirm 后二次上传继承同一 trace）→挂账**（§6-①），
  不拦本批；本批如实记录该边界：弹窗前后是两张证

### 2.2 raw_output（最小合规，§6-②）

- 内容：解析后的**结构化判断输出**（白名单字段序列化），非任意原文回包
- 截断：超 2048 字符截断+标记
- 对抗测试：模型输出故意复述合同句子→断言落账内容不含该片段
- 保存期限随 llm_calls 既有 TTL；查询与账本同权（代码层 query() 增 trace_id 过滤）
- 权限后台/复杂脱敏平台不建（§6-②）

### 2.3 prompt_hash

实际发送提示词全文 SHA256 前 16 位（哈希无泄露面）；新列与 1.1 迁移一并落地。

## 3. 测试要求（门禁验收基线）

1. 迁移：旧 schema fixture→升级→按列名读回历史行→写新行按列名读回（§1.1.2）
2. 结算：合法 JSON 白名单外→账本终态 parse_failed（无 success 假绿）；非 JSON→
   provider_error；settle 禁改写 provider_error/timeout；解析路径异常仍必结算
3. 预算：额度耗尽→零网络请求断言+SQLite 存在 budget_exceeded 行（emit_skip 实证）+
   重试前二次扣减+关闭态直通
4. 隐私对抗：复述合同句子→落账无该片段（§2.2）
5. 五路径+建议层 trace_id 非空且可按 trace_id 聚合查询；模块无自生成入口
6. **审查单锚点反查（终审 P1-1 验收）**：正常上传完成后，从最终 review_id 出发
   读到 trace_id，并按该 trace 查到建议层、预审与后台审查的账本行；
   回填不重复不遗漏
7. 行为零变化守卫：开关关+预算关时上传响应与现状一致（全量回归为据）


## 4. 验收门

1. ruff+pyright（0 错误基线）+全量测试绿
2. §3 清单全绿
3. 部署后 fail-loud 核验：建议层账本行 trace_id/prompt_hash 非空、结算后
   raw_output 非空
4. ChatGPT 复批 v0.5 + 用户确认合并后开工

## 5. 风险与回滚

- 迁移失败→emit 吞异常不伤主链；部署 SOP 先备份账本（已入册）
- 代码回退→必须伴随 schema 回退（§1.1.3，禁止只回代码）
- 预算误拦→回退=关建议层开关；额度语义不碰
- trace 边界：弹窗续办挂账期间，建议层与最终审查分属两张证（如实记录，非缺陷掩盖）

## 6. 挂账（ChatGPT 收敛裁定：不阻塞本批施工）

| # | 事项 | 去向 |
|---|---|---|
| ① | category_confirm 续办令牌（HMAC 绑 trace+文件哈希，二次上传继承原证） | 后续体验/关联增强批 |
| ② | raw_output 保存期限独立策略/查询权限后台/复杂脱敏平台 | 正式开放配置批 |
| ③ | 生产 `<=0` 强制拒绝+危险开关审计 | 正式开放配置批（本批文档写明生产必须正数） |
| ④ | chat_completion 存量节点结算化改造（§1.2.4 范本已备） | 另批 |
| ⑤ | /ask 预算、全局日限额 | 既有挂账不变 |

## 7. 放行条件

1. ChatGPT 复核 v0.5 确认终审双 P1 收口+三件硬事落位
2. 用户确认合并本施工卡
3. §3 测试清单为施工门禁验收基线