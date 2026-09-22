# 证据法批 2b-② 专项实现稿：claim_id + claim_content_hash + evidence_refs

> 状态：**v1.3 待复审**（2026-09-22）。v1.2 三阻塞已认可修掉；复审再补三个设计漏洞（组聚合按完整记录排序 / rebuts 字段入 schema 契约 / content_schema_version 字节格式），+ Q1 title 聚合，本版全部落实（对账表 §8 v1.3 节）。总设计契约：docs/evidence-batch2-design.md v1.1 + 批 2b 实现稿 v1.3。
> 前置：2b-① 已正式验收通过（审计 2026-09-22，main=38eb2ba，857 passed + E2E）。
> 分隔符记法：`<US>`=U+001F 单元分隔符、`<RS>`=U+001E 记录分隔符（实现用真实控制字符，本文为可读性记名）。
> 节奏：**只审设计、不直接开工**。本稿逐条回应审计 8 条约束 + 兑现两个「开工前再确认项」。

## 0. 范围一句话与批次边界（约束 8）

**本批做**：给五类主张发 `claim_id`（+ `claim_content_hash`），并挂上**带关系类型的证据引用边** `evidence_refs`（实际产生 primary / rebuts 两类）。
**留给 2b-③**：counter_evidence 票据化（含 absent/missing 分离）、Ask 引用入库、三路并发写入测试。
**永不属于 2b**：citation_edges（2c 与 decision_history 同批）、supports/context（枚举预留，2b 不产生——无 fact_id 关联来源，不让程序猜关系）。

边界声明：本批 **不触碰** 2b-① 已交付的证据坐标/收敛/索引机制，只在「已收敛的票据」之上叠加主张侧标注。

## 1. 审计八条约束逐条落实

### 约束 2：claim_id 必须在证据规范化完成后生成；身份禁下标/禁展示顺序

- **写路径时机**：pipeline 落库前统一规范化（2b-① 交付的 canonical 化）**之后**追加主张标注步骤——claim_id 派生自**已收敛票据**的 evidence_id，不存在「先编号后换证」。
- **读路径补齐**：旧记录（无 claim_id）由 normalize 循环用**同一公式**派生补齐——写/读双路径同源（总设计稿 Q1 裁决），无漂移。
- **业务主键（禁下标实证继承）**：v1.1 已实测废除 `source_ref=f"obs:{i}"`（verify.py:386）；本批 source_ref 全线降级为纯展示字段，任何派生路径不得引用。

```text
claim_id = "cl-" + sha256(document_version + "\x1f" + claim_type + "\x1f" + 业务主键)[:12]
```

| claim_type | 业务主键（全部为稳定标识，零下标/零截断/零模型顺序） |
|---|---|
| rule_item | **item_id + "<US>" + primary evidence_id**（v1.2 阻塞一修订：同一段原文可同时触发两条规则——只看证据会把「付款规则命中」和「验收规则命中」并成同一主张。item_id 取规则配置稳定 ID，绝不取数组位置） |
| blind_candidate | 库内 id + "\x1f" + primary evidence_id |
| quality_observation | dimension + "\x1f" + primary evidence_id（同键多条观察=同一主张，合并语义与组聚合指纹见 §2-B） |
| verify_question | source + "\x1f" + primary evidence_id（见 §2-A 再确认项） |
| objection | item_id + "\x1f" + direction + "\x1f" + primary evidence_id |

### 约束 3：关系类型服务端结构派生，不信模型声明

- `relation ∈ {primary, supports, rebuts, context, counter}`；2b-② 实际产生 **primary / rebuts** 两类：
  - **primary**：主张的内嵌合格票据（结构位置决定，服务端）
  - **rebuts**：仅**通过五要件受理**的异议（`accepted=True`）→ 所争议规则项的主证据（Q4 裁决；口径纪律：accepted=「准许复核」，≠人工采纳≠异议成立——字段注释与文案统一）
- 模型输出的任何关系声明字段一律忽略（与批 1 同源安全边界）；关系由代码根据「这个对象在结构里是谁」判定。
- **evidence_refs 只收合格票**：每个元素 `{evidence_id: 非空且形状合法, relation}`；按 `(evidence_id, relation)` 去重、按 `(relation, evidence_id)` 固定排序——同一主张任意路径派生 refs 逐字节一致。
- **一主张至多一个 primary**（不变式，测试钉）；所有 refs 引用必须与主张属**同一 document_version**（跨版本引用=身份错乱，校验 + 测试钉）。
- **relation 严格枚举**：`relation = Literal["primary","supports","rebuts","context","counter"]`，Pydantic 层面约束（非裸字符串）；`verify.source` 白名单 = `quality_obs|blind|pending|fact|rule_attention`，`quality.dimension` 白名单 = `completeness|consistency|impact`——非法值**不进入身份计算**（claim_id 留空 + 登记簿 broken 账目）。

### rebuts 的 fail-closed（v1.2 阻塞三修订——目标无证据怎么办）

被异议规则项「未找到」或主证据不合格时（漏报型异议的常见场景）：

```text
目标规则项无合格主证据
  → 不生成 rebuts 边（绝不伪造空 ID 指向不存在的证据——「假链接」防线）
  → 异议对象记录：rebuts_status="missing"、rebuts_reason="target_no_valid_evidence"
  → 异议自己的有效证据照常生成 primary 边（漏报异议的立身之本）
```

- rebuts_missing 账目进登记簿 broken_refs（reason 枚举扩充，随本批登记）。
- 裁决记录（审计 Q3）：**单向边**获赞成——rebuts 只挂异议侧，被反驳的规则项不感知（铁律 3 物理隔离）；fail-closed 规则如上。

### 约束 4：无有效证据的主张不发正式 claim_id

- 主张无合格主证据（票据 missing/unverified/ID 空）→ `claim_id=""`、`claim_content_hash` 照常计算（内容指纹不依赖证据）、`evidence_refs=[]`。
- 空串永不参与身份计算（防「无证据主张撞号」）；此类主张在登记簿 broken 账目可见（2a 自检覆盖）。

### 约束 5：counter_evidence 语义（2b-③ 交付，本批只定契约位）

- 本批 objections 对象**不新增** counter 相关字段（防范围扩大）；absent/missing 分离、`counter_evidence_ref`、`state/reason` 结构按批 2b 总稿 v1.2 §1.5 契约在 2b-③ 实施。

### 约束 6：Ask/Objection/Verify 三类写入的索引合并与并发防覆盖

- **Verify**：2b-① 已接线（trigger/confirm/reverify 三端点锁内 `_merge_evidence_index`）——本批不变。
- **Objection**：pipeline 内生成（单 worker，与索引构建同阶段，无并发）；done 后唯一写方 adopt 只改 `adopted` 布尔键、不产新票不动索引——**无需合并调用**，本批显式声明此结论并有测试钉（adopt 后索引逐字节不变）。
- **Ask**：不落库（2b-③ 才入库），本批无写方。
- **本批新增写方**：claim_id 标注发生在 pipeline 规范化之后（单线程段）与 normalize 读路径（纯读）——**均不引入新的并发面**。
- **三路并发测试（Ask+Verify+Objection）**：唯一真正的并发源是 2b-③ 的 Ask 入库，随 2b-③ 交付（挂账显式声明，不在本批虚交）。

### 约束 7：2b-① 老票据迁移对 2b-② 的影响

- 2b-① 的 ID 重算发生在**读路径归一化**（旧记录每次 GET 修复并记账）。claim_id 派生自**归一化后**的 primary evidence_id → 旧记录补齐的 claim_id 与重算后的证据 ID 天然一致，**无跨批漂移**。
- 同一条主张两次 GET（中间无写入）→ claim_id 幂等（归一化本身幂等，2b-① T3 钉过）；一旦 2b-① 迁移使 evidence_id 变化（只发生在旧数据首次修复），claim_id 随之稳定到新值——由 `id_recomputed` 警告与 claim 变化共同记账，不做跨批映射（`supersedes_claim_id` 契约预留，批 3）。

## 2. 两个「开工前再确认项」（终审点名）

### A. verify_question 稳定业务主键的精确口径

```text
verify_question 主键 = source + "\x1f" + primary_evidence_id
```

- **合并语义声明**：`source + 证据` 相同的两条核验问题视为**同一主张的多个表述**（问题措辞漂移不改身份，措辞变化由 content_hash 捕捉）。理由：核验问题的实体是「针对这张证据的这个来源的疑点」，措辞是表述不是身份。
- `source` 取值域固定（`quality_obs | blind | pending | fact | rule_attention`，verify.py:44 注释即枚举）；**严格白名单**——非法 source 的主张不发 claim_id、进 broken 账目（不静默入身份）。
- **Q1 裁决落实（合并主张的四个细则）**：
  1. 多条不同问法**全部保留**（展示不删除）
  2. 默认展示文本 = 全部问法的**字典序最小者**（确定性、与输入顺序无关，§1.7 同哲学）
  3. `claim_content_hash` 按**合并后的主张**统一计算——question 作为列表字段，排序后 `<RS>` 连接（每条问题单独算 hash 会让「同一主张」出现多个指纹，违背主张级身份）
  4. source 白名单如上，非法值不入身份
  5. **title 聚合（v1.3 补齐）**：title 不单独聚合——与 question 绑定为完整记录（`question=<…><US>title=<…>`）参与组排序（§2-B 聚合规则）；默认展示与 hash 同一套确定性规则
- 反例对账：同 source 不同证据 → 不同 claim；同证据不同 source → 不同 claim；`obs:{i}` 永不入键。

### B. claim_content_hash 的标准序列化（v1.2 重设计——阻塞二修订）

v1.0 的 `sorted([f1, f2, ...])` 有字段互换漏洞：title 与 comment 内容互换后 hash 不变——「有哪些文字」被当成了「文字没变」。v1.2 改为**带字段名、固定字段顺序**的序列化：

```text
claim_content_hash = "cc-" + sha256(serialized.encode("utf-8"))[:12]

serialized = content_schema_version + "" + 按固定字段顺序的 "字段名=值" 串
  - 普通单值字段：不排序，按下方白名单的固定顺序排列
  - 真正的列表字段（仅合并主张产生）：字段内排序后以 "<RS>" 连接
  - 每个值先 strip()；schema 版本参与哈希（字段清单演进时 +1）
```

**content_fields 白名单（v1.2 扩充；v1.3 澄清 schema_version 不在字段表内）**：

| claim_type | 固定顺序字段（白名单） | 说明 |
|---|---|---|
| rule_item | name, note | name 参与漂移检测 |
| blind_candidate | name, note | — |
| quality_observation | title, comment | 合并组按完整记录聚合（见上） |
| verify_question | question, title | 合并组按完整记录聚合（title 与 question 同记录绑定） |
| objection | legal_reasoning, proposal, stance_check | — |

**content_schema_version 的最终字节格式（v1.3——只出现一次、作为前缀键）**：

```text
serialized = "schema_version=cc1<US>" + 按白名单固定顺序的 "字段名=值" 串（<US> 连接）
```

  - 版本号**只出现一次**，固定键名 `schema_version`，固定值 `cc1`（字段清单演进 → cc2），绝不作为白名单字段重复写入
  - 值内控制字符转义：U+001F（<US>）/ U+001E（<RS>）在合同/模型文本中属数据损坏，序列化前**统一剥除** U+0000–U+001F（不做转义保留——保留转义会让「值里带分隔符」与真分隔符歧义）
  - 空值表示：`字段名=`（等号后为空串），不省略字段
  - 最终统一 UTF-8 编码后取 sha256

- `quote` 不进 content_hash——它已由 evidence_id 约束（审计 Q2 确认），不重复计量。
- **交换测试**：title/comment 互换 → hash 必变（新增钉子，杀死 v1.0 漏洞）。
- **合并组的聚合规则（v1.3 修订：按「完整记录」排序，不是按字段分别排序）**：v1.2 的「title 一摞、comment 一摞分别排序」会丢失字段对应关系（观察 A 的 comment 换成 B 的，两摞集合都不变、hash 不变——「姓名和成绩分两摞，不知道哪个成绩属于谁」）。改为：

```text
第一步：每条成员记录序列化为完整行：
    record_i = "title=<该条title>" + "comment=<该条comment>"
第二步：完整记录整体排序（字典序），以 <RS> 连接：
    records = record_1<RS>record_2<RS>...
第三步：带字段名固定顺序序列化：
    serialized = "schema_version=cc1" + "records=" + records
```

  - **排序单位是「整条记录」**——字段对应关系完整保留；交换两条观察的 comment → 组 hash 必变；仅交换数组顺序 → hash 不变（两条测试钉，T5g/T5h）
  - 单条观察 = 组大小 1 特例（同一公式，records 只有一条）
  - verify_question 合并组同口径：每条完整记录 = `"question=<…>title=<…>"`——**title 与 question 同记录绑定聚合**（Q1 裁决：不做 title 单独聚合），默认展示文本仍取 question 字典序最小者，hash 与展示用同一套确定性规则

## 3. API 契约与兼容

- schemas 新增：`EvidenceEdgeInfo {evidence_id: str, relation: Literal["primary","supports","rebuts","context","counter"]}`（严格枚举，非裸字符串）；`Objection` 模型增 `rebuts_status: Literal["present","missing","not_applicable"] = "not_applicable"` / `rebuts_reason: str = ""`（**显式入 schema 防静默剥字**——批 1 Codex P1 教训）；五类主张模型各增 `claim_id: str = ""` / `claim_content_hash: str = ""` / `evidence_refs: list[EvidenceEdgeInfo] = []`（只加不删，旧客户端零感知——批 1「schema 静默 ignore」教训的反向应用，先立契约）。
- 兼容矩阵：旧记录（无新键）→ 读路径补齐，行为与今天逐字节一致（新增字段之外）；新字段 None/空语义对前端/docx 无感。**读路径补齐只计算、不写回 store**（派生值仅存在于响应副本——store 行逐字节对照测试延续 2b-① 红线）。
- **铁律 3 测试钉**：claim/evidence_refs 全为主张侧标注——pipeline 落库前后 `items[].status` 逐字节对照 + Design B 既有断言照跑。

## 4. 测试清单（tests/test_evidence_claims.py）

| # | 测试 | 断言核心 |
|---|---|---|
| T1 | 稳定性三连 | 同主张两次生成同 claim_id；同批观察**仅换序** → 全部 claim_id 不变；改一个字 → content_hash 变而 claim_id 不变 |
| T2 | verify_question 主键 | 同 source 同证据 → 同 claim；source_ref（obs:i）注入篡改 → claim_id 不变（下标不入身份） |
| T3 | 无主证据不发正式编号 | 两条无合格证据的主张 → claim_id 均为空串（**不要求空值互异**）、不进任何 evidence_refs、不登记为有效主张；broken 账目可见 |
| T4 | refs 去重定序 | 同主张多路径派生 → refs 逐字节一致；含不合格票 → 不进 refs |
| T5 | rebuts 仅受理 | accepted=True 异议才有 rebuts 边；rejected 无；accepted≠采纳口径断言 |
| T5b | rebuts 目标无证据（阻塞三） | 目标规则项「未找到」→ 无 rebuts 边 + rebuts_status=missing + reason=target_no_valid_evidence + 异议自身 primary 照常 |
| T5c | 端点归一同引用同 ID | 坐标路径与 locate 路径产同 ID（2b-① 钉 4 延伸） |
| T5d | 一主张一个 primary | refs 中 relation=primary 至多 1 条 |
| T5e | 引用同版本 | refs 中 evidence_id 对应票据的 document_version 与主张一致 |
| T5f | content_hash 交换测试（阻塞二） | 同一条观察 title/comment 内容互换 → hash 必变 |
| T5g | 组聚合记录交换（v1.3） | 组内两条记录的 comment 互换 → 组 hash 必变（字段对应关系不丢） |
| T5h | 组聚合顺序无关 | 仅交换数组顺序 → 组 hash 不变 |
| T5i | schema 回归（v1.3） | rebuts_status=missing 场景 → 两字段真实到达客户端（非被 pydantic 剥掉）；accepted=False → not_applicable |
| T5j | schema_version 单次出现 | 序列化串中 `schema_version=` 恰好一次、位于最前 |
| T6 | 旧记录补齐 | 无 claim_id 旧行 → 读路径补齐且与写路径公式重算一致 |
| T7 | 迁移一致性 | 老票据（估算端点）读路径修复后 claim_id 稳定到新证据 ID；两次 GET 幂等 |
| T8 | adopt 不动索引 | adopt 后 evidence_index 逐字节不变（约束 6 的声明钉） |
| T9 | schema 契约 | 五模型新字段齐备；旧客户端忽略不受影响 |
| T10 | 铁律 3 | 落库前后 items[].status 逐字节不变 |

## 5. 变异验证计划（红线：回滚必须变红）

| 变异 | 预期红 |
|---|---|
| M1 claim_id 派生混入数组下标 | T1/T2 |
| M2 无主证据仍发 claim_id（空串参与） | T3 |
| M3 refs 收录不合格票 / 去重定序摘除 | T4 |
| M4 rebuts 不校验 accepted | T5 |
| M5 读路径不补齐旧记录 | T6 |
| M6 content_hash 字段名缺失（互换掩盖回归） | T5f 交换测试 |
| M7 rebuts 目标无证据仍生成边 | T5b |
| M8 组聚合按字段分摞排序（v1.3 漏洞回归） | T5g |
| M9 rebuts 字段缺席 Objection schema | T5i |
| M10 schema_version 重复/缺席 | T5j |

## 6. 载荷与回滚

- evidence.py（claim 派生函数 + normalize 接线）、pipeline.py（规范化后标注步）、schemas.py（+2 模型字段）、五类产出点轻接线、tests +1 文件——单分支 2~3 commit，可独立 revert。
- 前端/docx 零改动（消费端 2c）。

## 7. 请审计确认的三点

1. **verify_question 合并语义**（§2-A）：「同 source 同证据=同一主张」是否接受？替代方案是再加 question 文本哈希入键——但那会让措辞漂移变成换号，与「身份≠表述」哲学冲突，我方不推荐。
2. **content_fields 白名单**（§2-B）：各类型的字段清单是否齐备/有无该进没进的展示字段（进清单即参与漂移检测）？
3. **rebuts 边的挂载位置**：挂在异议对象的 evidence_refs（我方方案）vs 同时回写规则项对象（形成双向边）？我方推荐单向挂异议侧——规则项是「被主张对象」，不该感知谁在反驳它（单向边保铁律 3 的物理隔离）。

## 8. v1.0 → v1.2 修订对账表

| 审计意见 | 落实 |
|---|---|
| 阻塞一 rule_item 主键合并误伤（P1） | §1 业务主键表：item_id + primary evidence_id（规则配置稳定 ID） |
| 阻塞二 content_hash 字段互换掩盖（P1） | §2-B 带字段名固定顺序序列化 + content_schema_version + 交换测试（T5f） |
| 阻塞三 rebuts 目标无证据未定义（P1） | §1 rebuts fail-closed：不生成边 + rebuts_missing 记录 + 自身 primary 照常（T5b） |
| Q1 verify_question 合并四细则 | §2-A：全保留/字典序展示/主张级统一 hash/source 严格白名单 |
| Q2 白名单扩充 + T3 表述 | §2-B 白名单表（name/stance_check 补入，quote 明确排除）；T3 改「不发正式编号」口径 |
| Q3 单向边赞成 | §1 rebuts 节裁决记录 |
| 非阻塞：relation 严格枚举 | §1 + §3（Pydantic Literal） |
| 非阻塞：source/dimension 白名单 | §1（非法值不入身份 + broken 账目） |
| 非阻塞：refs 禁空 ID / 单 primary / 同版本 | §1（不变式三条 + T5d/T5e） |
| 非阻塞：读路径只计算不写回 | §3（声明 + store 逐字节测试延续） |
| 非阻塞：并发/旧数据/无目标证据测试 | T5b/T6/T7 + 三路并发随 2b-③（约束 6 已声明） |

### v1.2 → v1.3 对账

| 复审意见 | 落实 |
|---|---|
| 漏洞一 组聚合字段分摞丢对应关系 | §2-B 完整记录排序（T5g/T5h 双钉） |
| 漏洞二 rebuts 字段未入 schema | §1 契约表 + §3 Objection 显式声明 + T5i（批 1 静默剥字同款测试） |
| 漏洞三 content_schema_version 字节歧义 | §2-B 最终格式：schema_version=cc1<US> 前缀单次出现 + 值内控制字符剥除 + 空值表示 + UTF-8（T5j） |
| Q1 title 聚合 | §2-A 裁决 5：与 question 绑定完整记录聚合，展示/hash 同一套规则 |
