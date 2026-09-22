# 证据法批 2b-② 专项实现稿：claim_id + claim_content_hash + evidence_refs

> 状态：**v1.8 待复审**（2026-09-23）。v1.7 四项已认可；终审两张图对齐式总检查再补四点，本版全落实：①总设计稿 §4.2/§4.7/§4.10 与本稿统一（analysis_scope 三元组为唯一正式口径；Decision 必含 claim_content_hash）②迁移警告入 ReviewSummary 响应 schema + 三重测试（可见/store 不变/幂等）③cited_by 与 citation_edges 归属终裁（2b-② 只做 evidence_refs，两者均留 2c）④版本哈希精确到字节（哈希对象/排序/编码/注释敏感性与 legacy 语义）。对账 §10 末节。历史：v1.6 四项接近可施工；终审再补四点，本版全落实：①迁移警告写入语义钉死（响应副本生成、绝不写 store，持久化留 2c 显式入口）②pending 无稳定对象键→不发正式 claim_id（措辞不当身份证）③analysis_scope 扩为三元组（+rule_engine_version 引擎代码哈希）④总设计稿批次拆分声明（2b=①+②+③）。对账表 §10 末节。历经 v1.0（八约束+两确认项）→ v1.1（施工顺序重排+六修订）→ v1.2（三阻塞+Q1-Q3+非阻塞）→ v1.3（组聚合完整记录 / rebuts 入 schema / schema_version 字节格式 / title 聚合）→ v1.4（身份边界四块）→ v1.5（scope 落库 / 统一命名空间 / 来源对象键 / 2c 硬验收）→ 本版 v1.6（全文一致性重写：§2-A 旧公式清理、legacy 账本与证据登记簿分账、quality scope 裁决方案 A、quality_obs 来源对象键、文档卫生）。
> 前置：2b-① 已正式验收通过（审计 2026-09-22，main=38eb2ba）。
> 节奏：**只审设计、不直接开工**。本稿为开工放行的唯一依据，与代码冲突时以通过评审的本稿为准。
> 分隔符记法：`<US>`=U+001F 单元分隔符、`<RS>`=U+001E 记录分隔符（实现用真实控制字符，本文为可读性记名）。

## 0. 范围一句话与批次边界（审计约束 8）

**本批做**：给五类主张发 `claim_id`（+ `claim_content_hash`），并挂**带关系类型的证据引用边** `evidence_refs`（实际产生 primary / rebuts 两类）。
**留给 2b-③**：counter_evidence 票据化（absent/missing 分离）、Ask 引用入库、三路并发写入测试。
**永不属于 2b**：citation_edges（2c 与 decision_history 同批）、supports/context（枚举预留，不产生——无 fact_id 关联来源，不让程序猜关系）。
**边界声明**：不触碰 2b-① 已交付的证据坐标/收敛/索引机制，只在已收敛票据之上叠加主张侧标注。
**cited_by / citation_edges 终裁（v1.8）**：2b-② **只生成主张对象上的 `evidence_refs`**；不派生 cited_by 索引；citation_edges（只追加历史账）完全留到 2c——三者是三层东西（对象内当前引用 / 聚合读视图 / 历史账本），后两层随 2c 一并交付（总设计稿 §4.3 已同步）。

## 0.1 施工顺序

```
2b-① 证据规范化与 span 复用（✅ 已合并 PR #70）
2b-② Claim ID / evidence_refs（本稿——身份证定稿后才发编号）
2b-③ counter_evidence 与 Ask 入库
```

## 1. 身份规则（claim_id）

### 1.1 公式与作用域

```text
analysis_scope = rule_pack_id + <US> + rule_pack_content_version + <US> + rule_engine_version

claim_id = "cl-" + sha256(
    document_version + <US> + analysis_scope + <US> + claim_type + <US> + 业务主键
)[:12]
```

**版本哈希的字节级规范（v1.8 终审——精确到字节，无解释空间）**：

| 版本 | 哈希对象 | 算法 | 注释/格式敏感性 |
|---|---|---|---|
| rule_pack_id | 品类 ID 字符串（如 `procurement`） | —（不哈希，直接参与拼接） | — |
| rule_pack_content_version | 该品类的 checklist YAML **文件原始字节**（UTF-8，单文件；若未来拆多文件，按路径字典序排序后字节拼接） | sha256 前 12 位十六进制 | **注释与格式变化也算**——内容哈希哲学：确定性优先，宁可信版本敏感也不做语义 diff |
| rule_engine_version | `app/services/checklist.py` **文件原始字节**（引擎主逻辑单文件；若未来拆依赖模块，哈希文件清单按路径排序拼接，清单本身登记在本表） | sha256 前 12 位十六进制 | 同上 |

- 全部以**文件原始字节**计算（UTF-8 按存储形态，不归一化换行）——审计可复算：拿到同一版文件任意一方都能重出同一哈希。
- **legacy 语义（精确）**：整个 `analysis_scope` 位置的字符串以 `"legacy"` **整体替代**（不是三元组各填 legacy）——即旧记录的 scope 串就是 `"legacy"`；同批旧行派生一致，且必然与新版本 scope 串不同。
- 无 `row["rule_pack"]` 的旧行 + 引擎代码版本推断：**一律不做推断**——推断就是「用今天的版本算昨天的号」，legacy 是唯一出口。

- **五类主张全部带 analysis_scope**（v1.6 裁决：quality_observation 采用方案 A）——质量 Prompt 实际接收 category / 规则结果上下文，观察并非脱离规则体系，统一带 scope 最安全。若未来出现真正的「文档级主张」，须作为新 claim_type 显式定义，不做隐式豁免。
- 作用域声明：文档 + 规则包双维度；跨合同、跨规则包、跨规则包版本的同名主张**必不同 ID**。

**scope 三元组的落库持久化**：pipeline 的 node_checklist 载入规则包时计算两个内容哈希，写进审查行 `row["rule_pack"] = {"id": <品类>, "content_version": <配置哈希>, "engine_version": <引擎代码哈希>}`。读旧记录用**行内保存的版本**派生 claim_id，绝不拿今天的规则重算昨天。旧记录无此键 → 兼容路径：scope 以字面量 `"legacy"` 参与 + 记 `claim_migration_warnings`（见 1.4）——不静默使用当前版本。2b-② 仅行内持久化 + 测试断言；API 外露 rule_pack 留 2c。

### 1.2 业务主键（全部稳定标识：零下标 / 零截断文本 / 零模型输出顺序）

| claim_type | 业务主键 |
|---|---|
| rule_item | item_id + <US> + primary evidence_id |
| blind_candidate | 库内稳定 id + <US> + primary evidence_id |
| quality_observation | dimension + <US> + primary evidence_id |
| verify_question | source + <US> + source_subject_key + <US> + primary evidence_id |
| objection | item_id + <US> + direction + <US> + primary evidence_id |

- `analysis_scope` 统一前缀（见 1.1），不再逐类型拼写。
- `source_ref`（如 `obs:{i}`，verify.py:386 实证下标身份）全线降级纯展示，任何派生路径不得引用。
- **source_subject_key 按来源取具体对象稳定键**：`rule_attention`→item_id；`blind`→补盲候选稳定 id；`fact`→事实规范化业务键（kind+value 哈希）；`quality_obs`→dimension + primary_evidence_id（即质量主张自身键）；**`pending`→无稳定服务端对象键（v1.7 修订：问题措辞即其唯一标识，拿措辞哈希当身份 = 改写措辞就换号，违反「身份≠表述」原则）→ pending 来源的核验问题不发正式 claim_id（留空），内容照常展示与计算 content_hash，进 2b-③ Ask 入库时若建立稳定 pending 对象键再补发**。**决定链禁令：无正式 claim_id 的主张，2c 不得为其写 Decision**——须待 2b-③ 建立稳定对象键后方可进入决定链（总设计稿 §4.8 已同步）。全部禁数组下标。
- 同段原文触发两条规则 → 两个 rule_item 主张（item_id 区分）；三个规则包同名 item_id → 三个不同主张（analysis_scope 区分）。

### 1.3 无有效证据的主张

主证据不合格（missing/unverified/ID 空）→ `claim_id=""`、`claim_content_hash` 照常算（内容指纹不依赖证据）、`evidence_refs=[]`。空串永不参与身份计算（多个无证据主张不得撞出同一假编号）；此类主张在登记簿 broken 账目可见。

### 1.4 claim_migration_warnings：主张侧迁移账本（与证据登记簿严格分账）

- 位置：row 内独立容器 `claim_migration_warnings`；元素 `{where, reason, detail}`。
- **写入语义（v1.7）与交付口径（v1.8 终审二选一裁决：选「入响应 schema」）**：迁移警告**只在响应副本中生成**（每次读取由行状态确定性重导出，幂等可预期），**绝不写回 store**。既然要给人看，就必须到达客户端——`ReviewSummary` 新增 `claim_migration_warnings: list[ClaimMigrationWarningInfo] = []`（元素 `{where, reason, detail}`），随 get_review 下发。
- **三重测试（v1.8）**：①旧行 GET 能看到 legacy_scope 警告（不被 schema 剥掉——批 1 静默剥字同款钉）②store 行逐字节不变 ③连续两次 GET 结果一致（除时间戳类字段）。
- 若 2c 需要迁移历史**持久化**，必须设计显式写入口（与 decision_history 同批、同锁纪律），不得由读路径偷偷落库；定位声明：2b-② 的它属于「响应派生诊断信息」（可重导出），非持久账本。
- 收录：`legacy_scope`（旧记录缺规则包版本）及后续批次的 supersedes/迁移类主张侧事件。
- **分账边界**：`evidence_registry.broken_refs` 只记**证据票据异常**（ID/坐标/资格）；主张侧身份与迁移事件一律进本账本——「身份证版本过期」不进「快递单损坏」的账本。
- 归一化/读路径对其只追加或校验，禁止删除改写（历史账本纪律同总设计稿 §4.1）。

### 1.5 内容漂移防护

- `claim_content_hash`：见 §2（序列化标准）。
- `claim_revision`：整数，本批恒 0，契约预留；`supersedes_claim_id`：本批不填值（规则修订映射体系属批 3）。

## 2. claim_content_hash 序列化标准（精确到字节）

### 2.1 单条与合并组统一路径（只有一条序列化路径）

```text
# 第一步：每条成员记录序列化为完整行（字段名=值，按白名单固定顺序，<US> 连接）：
record_i = "title=<…>" + <US> + "comment=<…>"
# 第二步：完整记录整体按字典序排序，<RS> 连接：
records = record_1 + <RS> + record_2 + ...
# 第三步：加版本前缀（只出现一次）：
serialized = "schema_version=cc1" + <US> + "records=" + records
claim_content_hash = "cc-" + sha256(serialized.encode("utf-8"))[:12]
```

- **单条主张 = 组大小 1 走同一路径**（records 只有一条完整记录）——不存在「单条直算」与「聚合」两条格式（T5k 逐字节相等钉）。
- **排序单位是完整记录**，不是字段分摞——字段对应关系不丢（T5g 交换必变 / T5h 顺序无关双钉）。

### 2.2 content_fields 白名单（固定顺序）

| claim_type | 记录内字段（固定顺序） |
|---|---|
| rule_item | name, note |
| blind_candidate | name, note |
| quality_observation | title, comment |
| verify_question | question, title |
| objection | legal_reasoning, proposal, stance_check |

- `quote` 不进 content_hash（已由 evidence_id 约束，不重复计量）。
- 合并组：每条成员各自成完整记录（verify 的 title 与 question 同记录绑定聚合，不做 title 单独聚合）；默认展示 = 完整记录字典序最小者（hash 与展示同一套确定性规则，title 不会跟错问题）。

### 2.3 字节级规则

- `schema_version=cc1` 前缀只出现一次、位于最前（字段清单演进 → cc2；T5j 钉）。
- 值内分隔符**可逆转义**：反斜杠 → 双反斜杠、U+001F → 文字「反斜杠+u001f」、U+001E → 「反斜杠+u001e」；**其余字符原样保留（含换行）**——「付款（换行）条件」≠「付款条件」（内容变化不得静默吞掉；T5l 双向钉）。
- 空值：`字段名=`（等号后空串），不省略字段。最终 UTF-8 编码。

## 3. 关系类型（evidence_refs）

```python
relation = Literal["primary", "supports", "rebuts", "context", "counter"]
```

- 2b-② 实际产生 **primary**（主张的内嵌合格票据，结构位置决定）与 **rebuts**（仅 `accepted=True` 的受理异议 → 所争议规则项的主证据；口径：accepted=「准许复核」，≠人工采纳≠异议成立）两类。
- 关系全部由服务端按结构位置判定；模型输出的任何关系声明一律忽略。
- **不变式**：refs 只收合格票（ID 非空且形状合法）；按 `(evidence_id, relation)` 去重、`(relation, evidence_id)` 固定排序；一主张至多一个 primary；所有引用与主张同 document_version。
- **rebuts 的 fail-closed**：目标规则项无合格主证据（漏报异议常见场景）→ 不生成 rebuts 边（绝不伪造空 ID 假链接）→ `rebuts_status="missing"` / `rebuts_reason="target_no_valid_evidence"` → 异议自身有效证据照常生成 primary。
- **rebuts 单向边**（审计 Q3 赞成）：只挂异议侧，规则项不反向感知（铁律 3 物理隔离）。
- **rebuts 字段入 schema 契约**：`Objection` 模型显式声明 `rebuts_status: Literal["present","missing","not_applicable"] = "not_applicable"` / `rebuts_reason: str = ""`——防「内部记账被 pydantic 静默剥掉」（批 1 同族教训；T5i schema 回归）。

## 4. 写路径与并发（约束 6）

- claim 标注发生在 pipeline 规范化之后（单线程段）与 normalize 读路径（纯读，只计算不写回 store）——不引入新并发面。
- **Verify**：2b-① 已接线锁内合并，本批不变。**Objection**：pipeline 内生成无并发；done 后 adopt 只改 adopted 键不产新票不动索引（T8 钉）。**Ask**：不落库（2b-③）。
- 三路并发测试（Ask+Verify+Objection）：唯一真并发源是 2b-③ Ask 入库，随 2b-③ 交付（不虚交）。
- **2c 硬性验收条件**：decision 结构必须包含 `claim_content_hash`（决定时点的主张内容指纹快照）——缺此不验收（指纹必须真正用于审计）。

## 5. 2b-① 迁移对 2b-② 的影响（约束 7）

claim_id 派生自归一化后的 primary evidence_id → 旧记录补齐的 claim_id 与重算后的证据 ID 天然一致，无跨批漂移；旧记录 scope 缺失走 `"legacy"` + claim_migration_warnings（1.4）。两次 GET 幂等。

## 6. API 契约与兼容

- schemas 新增：`EvidenceEdgeInfo {evidence_id: str, relation: Literal[...]}`（严格枚举）；五类主张模型各增 `claim_id=""` / `claim_content_hash=""` / `evidence_refs=[]`；`Objection` 增 `rebuts_status` / `rebuts_reason`；`ReviewSummary` 增 `claim_migration_warnings`（§1.4，响应派生诊断）。只加不删，旧客户端零感知。
- 读路径补齐**只计算、不写回 store**（响应副本；store 行逐字节测试延续）。
- 铁律 3：claim/evidence_refs 全为主张侧标注——落库前后 items[].status 逐字节对照 + Design B 既有断言照跑。

## 7. 测试清单（tests/test_evidence_claims.py）

| # | 测试 | 断言核心 |
|---|---|---|
| T1 | 稳定性三连 | 同主张两次生成同 ID；同批观察仅换序 → 全部 claim_id 不变；改一字 → content_hash 变而 ID 不变 |
| T2 | verify 主键 | 同 scope+source+对象+证据 → 同 claim；source_ref 注入篡改 → ID 不变 |
| T2b | 规则包命名空间 | 同合同同证据同 item_id、不同 rule_pack → 不同 claim_id |
| T2c | verify 来源对象键 | 同 source 同证据、不同来源对象 → 不同 claim；同对象 → 稳定 |
| T2d | 版本落库与旧行兼容 | scope 三元组（配置+引擎哈希）参与 ID；旧行 → legacy + claim_migration_warnings（响应可见）+ 幂等 + store 不变 |
| T3 | 无主证据 | claim_id 均为空串、不进 refs、不登记为有效主张 |
| T4 | refs 去重定序 | 多路径派生逐字节一致；不合格票不进 refs |
| T5 | rebuts 仅受理 | accepted=True 才有 rebuts；rejected 无 |
| T5b | rebuts 目标无证据 | 无边 + rebuts_status=missing + reason + 自身 primary 照常 |
| T5f | 交换测试 | 同一记录 title/comment 互换 → hash 必变 |
| T5g | 组聚合记录交换 | 组内两条记录 comment 互换 → 组 hash 必变 |
| T5h | 组聚合顺序无关 | 仅换数组顺序 → 组 hash 不变 |
| T5i | schema 回归 | rebuts missing 场景两字段真实到达客户端；accepted=False → not_applicable |
| T5m | 迁移警告三重钉（v1.8） | 旧行 GET 可见 legacy_scope 警告（不被剥）；store 逐字节不变；两次 GET 一致 |
| T5j | schema_version 单次 | 序列化串中恰一次、位于最前 |
| T5k | 单条=组1 同路径 | 单条直算 == 聚合函数单成员组输出（逐字节） |
| T5l | 控制字符可逆 | 仅差换行的两值 hash 不同；含分隔符字面量值转义往返一致 |
| T8 | adopt 不动索引 | adopt 后 evidence_index 逐字节不变 |
| T9 | schema 契约 | 新字段齐备；旧客户端忽略不受影响 |
| T10 | 铁律 3 | 落库前后 items[].status 逐字节不变 |

## 8. 变异验证计划（回滚必须变红）

| 变异 | 预期红 |
|---|---|
| M1 派生混入数组下标 | T1/T2 |
| M2 无主证据仍发 ID（空串参与） | T3 |
| M3 refs 收不合格票 / 去重定序摘除 | T4 |
| M4 rebuts 不校验 accepted | T5 |
| M5 读路径不补齐旧记录 | 旧记录补齐组测试 |
| M6 content_hash 字段名缺失 | T5f |
| M7 rebuts 目标无证据仍生成边 | T5b |
| M8 组聚合字段分摞排序 | T5g |
| M9 rebuts 字段缺席 schema | T5i |
| M10 schema_version 重复/缺席 | T5j |
| M11 漏规则包命名空间 | T2b |
| M12 单条/聚合双路径 | T5k |
| M13 值内控制字符剥除 | T5l |
| M14 旧行静默用当前版本 | T2d |
| M16 迁移警告被 schema 剥掉 | T5m |
| M15 verify 丢来源对象键 | T2c |

## 9. 载荷与回滚

evidence.py（claim 派生 + normalize 接线）、pipeline.py（规范化后标注步 + rule_pack 落库）、schemas.py（+模型字段）、五类产出点轻接线、tests +1 文件；单分支 2~3 commit 可独立 revert；前端/docx 零改动。

## 10. 历轮对账（审计可核）

- **v1.0→v1.1**：施工顺序重排（证据先规范后编号）；禁下标身份（source_ref 实证降级）；无主证据不发号+content_hash；refs/edges 分工（edges 推迟 2c）；空引用不伪装；evidence_index 缓存治理
- **v1.1→v1.2**：阻塞一 rule_item +item_id；阻塞二 content_hash 带字段名固定顺序+交换测试；阻塞三 rebuts 目标无证据 fail-closed；Q1 合并四细则；Q2 白名单扩充+T3 口径；Q3 单向边；非阻塞全清（枚举/白名单/三不变式/只算不写）
- **v1.2→v1.3**：组聚合完整记录排序（T5g/h）；rebuts_status/reason 入 Objection schema（T5i）；schema_version 字节格式（T5j）；verify title 绑定聚合
- **v1.3→v1.4**：规则包命名空间（T2b）；单条=组1 同路径（T5k）；分隔符可逆转义（T5l）；verify 展示完整记录字典序
- **v1.4→v1.5**：rule_pack_version 落库+legacy 兼容（T2d）；analysis_scope 统一进 blind/objection/verify；source_subject_key（T2c）；content_hash 列 2c 硬验收；残句清理
- **v1.5→v1.6**：全文一致性重写——§2-A 旧公式清理与主键表统一（施工图不得两页尺寸）；legacy 账本迁出证据登记簿（claim_migration_warnings 独立分账）；quality scope 裁决方案 A（五类全带 scope）；quality_obs 的 source_subject_key 补齐；状态头/围栏/残句等文档卫生

### v1.6 → v1.7 对账

| 终审意见 | 落实 |
|---|---|
| 1 迁移警告「只追加」vs「读路径只读」冲突 | §1.4 写入语义钉死：2b-② 仅响应副本生成、绝不写 store；持久化留 2c 显式写入口 |
| 2 pending 拿措辞当身份 | §1.2 source_subject_key 映射：pending 无稳定对象键 → 不发正式 claim_id（留空），不拿措辞顶替身份证 |
| 3 rule_pack_version 不覆盖执行代码 | §1.1 analysis_scope 扩三元组：+rule_engine_version（checklist.py 内容哈希）；row["rule_pack"] 三键落库 |
| 4 总设计稿与专项稿批次口径差 | 总设计稿 §5-2b 增批次拆分声明（2b=①+②+③，各子批边界与交付物标注） |

### v1.7 → v1.8 对账

| 终审意见 | 落实 |
|---|---|
| 1 两张图 claim_id 公式不一致 / Decision 缺 content_hash | 总设计稿 §4.2（三元 scope 唯一口径）/ §4.7（+claim_content_hash）/ §4.10（双维作用域）/ §4.8（快照硬验收 + pending 决定链禁令）全部对齐 |
| 2 claim_migration_warnings 谁看未闭环 | 裁决「入响应 schema」：ReviewSummary.claim_migration_warnings + 三重测试（可见/store 不变/幂等，T5m）；定位=响应派生诊断，非持久账本 |
| 3 cited_by vs citation_edges 归属冲突 | 终裁：2b-② 只做 evidence_refs；cited_by 索引与 citation_edges 历史账均留 2c（两稿 §4.3/§0 同步） |
| 4 版本哈希精确到字节 + legacy 语义 | §1.1 字节级规范表：哈希对象/文件排序/编码/注释敏感性/legacy=整串替代；pending 决定链禁令（§4.8 同步） |
