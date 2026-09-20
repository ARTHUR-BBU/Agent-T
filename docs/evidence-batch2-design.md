# 证据法批 2 设计稿：Evidence → Claim → Decision 引用化改造

> 状态：**v1.1 草案待审**（2026-09-20）。v1 经审计评审「方向通过，方案暂不定稿」，本版按裁决修订：登记簿与历史记录分离、claim_id 作用域、cited_by 以 claim_id 为主键、引用关系类型、Decision 完整结构、决定历史只追加、Ask 入库隐私 TTL、服务端 span 复用规则、ID 作用域声明。
> 前置：批 1（evidence_id 身份证）已正式签收（PR #66，main@cd372bb）。

## 一、目标（一句话）

让「事实、主张、正式决定」都真正**引用**同一张证据身份证，而不是各自重新抄一遍原文——把批 1 隐式的内容哈希同一性，升级为显式的引用边（citation edge）。

## 二、现状地图（摸底结论，2026-09-19 @ cd372bb）

### 2.1 票据生成：七层全部「自建票」，无一引用

| 层 | 生成点 | parse_source | 现状 |
|---|---|---|---|
| 规则 checklist | pipeline.py:135 → attach_evidence_to_item | rules | 自建（quote 来自引擎 occurrence） |
| 补盲 blind | pipeline.py:207 → attach_evidence_to_item | blind | 自建（quote 来自模型输出） |
| 质量 quality | quality.py:449 → build_evidence | quality | 自建（quote 过全文校验） |
| 追问 ask | llm_ask.py:302-322 → build_evidence×2 | ask | 自建；**票据只随响应返回，不入 store** |
| 事实 facts | facts.py:79 / 183 | fact | 自建（两处） |
| 核验 verify | verify.py:569-603, 772-789 | **来源层**（verify.py:562 传递） | 自建但 parse_source 沿用来源层——同一 quote 与来源层同 ID（唯一有意识的跨层对齐） |
| 异议 objection | objection.py:587 → build_evidence | objection | 自建；对模型申报 quote 重新定位，**未引用所异议规则项的票据** |

### 2.2 evidence_id：全链路下发、零消费

- 7 个响应模型带 EvidenceRefInfo（schemas.py:49/64/135/144/174/258/341）
- 前端只读 clause_id 跳原文（app.js:427-443），**不读 evidence_id**
- docx 导出只读 quote（report.py:229/250），**不读 evidence 任何字段**
- store 无按 evidence_id 的索引/查重（单表 JSON，行级 TTL）

### 2.3 Claim / Decision 雏形

- **已是主张**：items[]、blind_candidates[]、quality.observations[]、verify.questions[]、objections[]——引用方式全部为票据内嵌 dict，无 ID 引用边。objection.counter_evidence 仍是裸字符串。
- **已是决定**：规则档位（引擎产出）、verify 的 human_choice/status、异议 adopted——决定作用于承载票据的对象快照，**没有「决定→主张→证据」的引用链**。

## 三、三层对象模型（设计）

```
Evidence（证据）      = 批 1 的票据，唯一事实源：quote + 坐标 + 版本 + evidence_id
Claim（主张）         = 一个可核查的断言：规则命中 / 质量观察 / 补盲候选 / 核验疑点 / 异议
Decision（决定）      = 人或引擎的裁决：规则档位 / verify 确认-争议 / 异议采纳 / 报告引用
```

引用边与记录（全部**只加不删**，向后兼容）：

```
Claim.claim_id: str                          # 主张稳定标识（作用域见 §4.2）
Claim.evidence_refs: [{evidence_id, relation}]  # 引用边，带关系类型（§4.5）
Decision（完整结构见 §4.7）                  # 可审计的决定记录，历史只追加（§4.8）
```

**总原则**：内嵌票据仍是事实源、不删除；引用边是叠加的关系层。旧记录无引用边 → 读路径按「内嵌票据派生」降级，行为与今天逐字节一致。

## 四、v1.1 新增规则（审计八条的落实）

### 4.1 登记簿与历史记录严格分离（裁决 1）

> 登记簿可以重建，决定历史不能重建。

| 容器 | 性质 | 重建策略 |
|---|---|---|
| `evidence_registry` | 当前索引（本案卷的目录） | **可重建**：读路径归一化时全量重建，字段含 `registry_version` / `rebuilt_at` / `broken_refs` |
| `citation_edges` | 引用账目（2b 起） | **只追加**：归一化只校验不删改；票据降级产生 `broken_refs` 记录而非删边 |
| `decision_history` | 决定历史（2c 起） | **只追加不可覆盖**（见 §4.8）；归一化永不触碰 |

归一化（normalize_review_evidence）的职责边界由此改写：只重建 `evidence_registry`；对 `citation_edges` / `decision_history` 最多做**校验与 broken 标记**，禁止删除或改写历史条目。

### 4.2 claim_id 作用域与版本（裁决 2）

```
claim_id = "cl-" + sha256(document_version + claim_type + 业务主键)[:12]
```

- **作用域声明：单审查记录内唯一**（同一份合同文本同 ID；跨合同同主张不同 ID）。evidence_id 同口径——两者都是**文档作用域短 ID**，未来跨文档空间必须升级为全系统长 ID 或加作用域前缀（规范文档远期条目，本批不做）。
- 规则词表修订后：旧审查记录保留旧 claim_id，新审查生成新 claim_id，**禁止悄悄复用/变脸**；预留 `supersedes_claim_id` 字段（本批不填、只定契约），claim_revision 映射体系留给规则变更提案通道（批 3 议题）。

### 4.3 cited_by 以 claim_id 为主键（裁决 3）

```json
{"claim_id": "cl-...", "layer": "quality", "relation": "supports", "path": "quality.observations[0]"}
```

- `claim_id` 是唯一身份关联键；`layer` 供人读；`path` 仅调试辅助，**不作为身份**。
- **2a 的处理**（v1.1 明确）：2a 不落地 claim_id，因此 2a 的登记簿**只做票据存在性账目**（每张票被哪几层引用的层级计数），不写 cited_by 关系边——「path 冒充身份」的账目一律不做。真正的 cited_by 边随 claim_id 在 2b 同批落地。

### 4.4 docx 纳入 2c，增量升级（裁决 4）

报告显示：原文摘句、证据状态、所在条款、引用它的主张、是否有人工决定、**证据无法定位时明确提示**；红线：报告不得把「未定位」写成「已核实」。配套四件：旧报告回归测试、新报告固定样例（golden file）、字段缺失降级显示、文案九哥把关。不做复杂灰度系统。

### 4.5 引用必须带关系类型（新增规则一）

```python
relation = Literal["primary", "supports", "rebuts", "context", "counter"]
```

- 每条 `Claim.evidence_refs` 元素必须声明关系：主证据 `primary`、支持 `supports`、反驳 `rebuts`、背景 `context`、反证 `counter`。
- **objection.counter_evidence 票据化时必须标 `counter`**（v1 的"第二张票"升级为显式反驳关系）；对所异议规则项的引用标 `rebuts`。
- 兼容期：内嵌票据 = 隐式 `primary`，读路径派生时自动补全。

### 4.6 同一证据由服务端 span 复用（新增规则二）

> 先由服务端把摘句定位到正文的真实 span，再决定是否复用已有 Evidence。不能让模型自己说"这是同一证据"。

- 复用判定流程：各层产出 quote → **服务端** `locate_quote_span` 得到规范 span → 按 `evidence_id_for(版本+规范span+条款)` 计算候选 ID → 命中登记簿即复用（叠加引用），未命中才新建。
- 摘句的标点/空格差异在 locate 的压缩匹配层被吸收（批 1 既有能力）；**模型输出的任何「同证据」声明一律忽略**——安全边界与批 1 一致：模型只产 quote，身份全由服务端派生。
- 多命中（ambiguous）：span 取首处，票据标 ambiguous，引用关系照常成立。

### 4.7 Decision 完整结构（新增规则三）

```json
{
  "decision_id": "dc-<sha256[:12]>",
  "decision_type": "human_confirm | human_dispute | objection_adopt | rule_engine | recheck",
  "actor": "user:<session标示> | system:rule_engine | system:recheck",
  "authority": "human | machine",
  "claim_id": "cl-...",
  "evidence_ids": ["ev-..."],
  "decision": {"choice": "confirm|dispute|adopted|status", "note": "...", "revised_quote": "..."},
  "decided_at": "<ISO8601>"
}
```

- `authority` 区分人与机器（审计口径：机器裁决永不冒充人工决定）；`actor` 可溯源（规则引擎 / 再核 / 用户会话）。
- verify 的问题级 `human_choice`/`status` 等「当前状态」字段保留不变（消费端兼容），`decision_history` 是其背后的完整账目。

### 4.8 决定历史只追加（新增规则四）

> 当前状态可以更新，决定历史必须追加。

- verify 的生命周期（待确认→已确认→再核→有争议）：每次转变**追加**一条新 decision 记录，`decision_id` 各自独立；从不覆盖旧记录。
- 当前状态字段与历史账目的一致性由读路径校验（历史末条应与当前状态吻合；不吻合标 `decision_history_divergent` 供审计，不静默改写）。
- 异议采纳同理：`adopted: true` 之外追加 `decision_type: objection_adopt` 记录。

### 4.9 Ask 入库的隐私与 TTL（新增规则五）

| 问题 | 规则 |
|---|---|
| 存什么 | **只存证据引用账目**：问题文本截断（≤200 字）+ 票据（ID/坐标/状态）+ quote_check 结果。**不存模型原始回答全文**——回答本来就不落库的隐私边界不因证据追踪而破坏 |
| 生命周期 | Ask 引用记录**寄生在审查行内**（row["ask_evidence"]），跟随行级 STORE_TTL 一起过期，无独立生命周期 |
| 再打开可见性 | 审查页可见「追问涉及 N 条证据」的计数，不回放旧对话 |
| 敏感内容 | 避免无限期保存：TTL=0（永不过期）时 Ask 引用记录**不登记**（与批 1「TTL=0 时无资格 ID 永续暴露」防线同哲学：宁缺毋滥） |

### 4.10 ID 碰撞与作用域（新增规则六）

- 12 位十六进制前缀（48 bit）在**单审查作用域**内（票据量 ≤ 千级）碰撞概率可忽略；批 2 明确声明：evidence_id / claim_id / decision_id 均为**文档作用域 ID**。
- 跨文档空间启动时（远期）统一升级：全系统长 ID 或 `scope:` 前缀——现在只把「作用域」写进规范与字段注释，不改 ID 长度。
- 读路径增加前缀防御：非 `ev-`/`cl-`/`dc-` 前缀或长度不符的 ID 视为 broken 引用，不参与关联。

## 五、分期实施（三小批，每批全绿独立合并）

### 批 2a：证据登记簿（地基）——只做「可重建的当前索引」

- **落地形态：纯读路径派生视图**（v1.1 收紧）：登记簿不落 store 行、不接写路径——get_review 归一化后由同一遍历派生，每次响应即时重建。「可重建」的最高形态就是「根本不持久化」：零写路径风险、零迁移、零 TTL 交互、且天然不违反「归一化不突变 store 行」的既有保证
- 视图契约（2a 实现稿 v1.1 修订）：`{registry_version, rebuilt_at, occurrence_total, unique_total, qualified_occurrence_total, qualified_unique_total, multi_source_unique, broken_ref_count, broken_refs[≤20]}`——出现数/唯一数分开计数；broken 账目在**归一化改写前**捕获（明细条目不下发——隐私与响应体积考虑）
- 语义边界：2a 只统计现有 evidence_id 的精确一致性，**不宣称语义级同证据合并**（quote 入哈希，标点差异即不同 ID；语义级复用是 2b 服务端 span 规范化的事）
- API：ReviewSummary 新增 `evidence_registry`（概览）；不写 cited_by 边（见 §4.3）
- 不做：新 SQLite 表、store 行持久化、claim_id、decision（后两批）

### 批 2b：Claim 标识 + 带关系类型的引用边（关系层）

- claim_id 派生（§4.2）落地各主张对象；`evidence_refs`（§4.5）替代 v1 的 `evidence_ids` 裸列表；cited_by 边（§4.3）随 claim_id 同批进登记簿
- objection.counter_evidence 票据化（`counter` 关系）+ 对规则项票据的 `rebuts` 引用
- Ask 证据引用入库（§4.9 全套规则）
- 服务端 span 复用判定（§4.6）接入各层生成路径

### 批 2c：Decision 引用链 + 报告闭环（裁决层）

- decision 完整结构（§4.7）+ 只追加历史（§4.8）：verify confirm/dispute/recheck、objection adopt
- docx 增量升级（§4.4）+ 前端「同证据多源」展示（evidence_id 不裸露给用户）
- 铁律 3 测试钉：所有 decision 写入路径断言 items 档位逐字节不变（照 Design B 先例）

## 六、验收标准（每批通用）

1. 全量测试绿 + 新行为钉子测试；**变异验证：回滚引用逻辑测试必须变红**（批 1 假绿教训成铁律）
2. 旧记录回归：无引用边的旧行（含 STORE_TTL=0）读路径行为与今天逐字节一致
3. 对抗场景：同一 quote 规则/质量/核验三层命中 → 登记簿一张票、三层计数；反证票据带 `counter` 关系可追溯到规则层原始票
4. fail-closed：引用不存在的 evidence_id → broken_refs 记录 + 降级，不抛裸异常、不删历史
5. **历史不可覆写**：变异验证——把「覆盖 decided_on」的旧实现回滚注入，测试必须抓住历史丢失
6. 每批独立 revert；归一化幂等性测试防重复登记

## 七、验收证据口径（裁决 5）

三层证据，生产截图仅作辅助：

1. **代码与测试证据**（主）：固定提交哈希 + 完整测试 + 变异测试
2. **部署环境冒烟**（主）：脱敏合同验证关键链路（证据链/报告/决定）
3. **生产自查截图/日志**（辅）：说明「某次检查看到过」，不宣称「系统一直如此」——对外表述统一此口径

## 八、明确不做（批 2 范围外）

- Evidence 跨审查记录共享 / 跨文档证据网络（远期）
- 向量检索 / RAG 证据权重（远期）
- 前端按 ID 的搜索筛选界面（登记簿先服务报告与追溯）
- ask 对话回放 / 会话持久化（既有挂账独立处理）
- LLM 直接引用或声明 evidence_id / claim_id（幻觉面——身份全服务端派生，模型只产 quote，批 1 已验证的安全边界不越过）
- 规则修订前后 claim_id 映射体系（`supersedes_claim_id` 只定契约，批 3 议题）

## 九、v1 → v1.1 修订记录

| 审计意见 | 落实 |
|---|---|
| 裁决 1 登记簿/历史分离 | §4.1（含 registry_version/rebuilt_at/broken_refs；归一化禁触历史） |
| 裁决 2 claim_id 作用域 | §4.2（document_version+type+业务键；supersedes 契约；禁变脸） |
| 裁决 3 cited_by 用 claim_id | §4.3（2a 不做 path 冒充身份的账目，边随 2b 落地） |
| 裁决 4 docx 入 2c | §4.4（增量升级 + 四件配套 + 「未定位≠已核实」红线） |
| 裁决 5 验收证据三层 | §七 |
| 新增规则一 关系类型 | §4.5（五类 relation；counter_evidence 必标 counter） |
| 新增规则二 服务端 span 复用 | §4.6 |
| 新增规则三 Decision 结构 | §4.7 |
| 新增规则四 历史只追加 | §4.8 |
| 新增规则五 Ask 隐私 TTL | §4.9（只存引用账目不存回答；TTL=0 不登记） |
| 新增规则六 ID 作用域 | §4.10 |
