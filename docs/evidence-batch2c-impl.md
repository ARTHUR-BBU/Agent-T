# 证据法批 2c 专项实现稿（轻量版）：决定引用主张 + 报告可追溯可读

> 状态：**v1.1 修订稿**（2026-09-27，按审计退回意见全文重写；v1.0 判词「方向正确，
> 暂不放行——2 P1 + 1 P2」：①决定后主张/证据变化无检测与降级语义 ②无 claim_id
> 的异议可留下 adopted=true 假状态 ③幂等只保证了编号。三根梁全部补齐，对账见 §9）。
> Q1-Q4 审计已裁决（§7，全部同意/有条件同意）。
> 治理冻结口径（roadmap 6.1.8 + 设计稿 v1.3）：**只做最小裁决闭环**，
> citation_edges / decision_history / cited_by 全量账后置冻结，解冻前不得施工。
> 摸底基线：main=5b6e214（生产运行 1249092，两者只差文档/工具）。
> 分隔符记法：`<US>`=U+001F（chr(31) 显式构造，禁字面隐形控制字符）。
>
> **诚实声明（外审 2026-09-27 要求）**：Ask「有效票据」的生产正向验证**尚未完成**
> ——本稿不依赖该能力，相关表述一律按挂账处理。

## 0. 范围一句话与不做清单

**本批做**（roadmap 6.1.8 轻量版四件事）：
1. **人工确认基于哪条主张**——confirm/dispute/adopt 写入「决定记录」，带 claim_id；
2. **决定引用哪条证据**——决定记录带决定时点的证据编号集 + 主张内容指纹快照（防编号不变内容被偷换）；
3. **报告可追溯**——docx 从结论追到主张编号、再到原文证据与可定位性；**含「决定后主张/证据变化」的如实降级显示**（v1.1 新增）；
4. **用户看得懂**——全部中文文案、无编号/无证据/内容已变化时明确说人话，不冒充。

**不做**（后置冻结，roadmap 6.1.8；任何一条出现在实现里即方向偏离）：
- citation_edges / decision_history 只追加账 / cited_by 聚合索引视图；
- 决定历史可回放（轻量版=当前状态记录，新决定替换旧决定）；
- 前端界面改动（决定字段先到，页面展示随报告闭环后的批次）；
- Ask 层决定链 / pending 稳定对象键（§3.4 预案已获批）；
- 规则档位任何形式的改写（铁律 3 永久红线）。

## 1. 现状摸底（2026-09-27 @ 5b6e214，全部代码实证）

| # | 事实 | 位置 | 对设计的影响 |
|---|---|---|---|
| 1 | confirm/reverify 只改问题级状态字段，无决定对象 | verify.py:727 apply_confirmation / recheck_question | 决定记录挂载点 = 对象新键 |
| 2 | **claim_id / claim_content_hash 是读路径派生的，store 里不存** | evidence.py annotate_review_claims | 写路径必须先派生 claim 再落决定（§3.2），本批最重技术点 |
| 3 | adopt 只翻 `adopted` 布尔键 | routes_objection.py:41-52 | v1.1 P1-2：采纳前置校验 claim_id |
| 4 | docx 报告今天完全没有主张编号/证据状态/决定内容 | report.py | 报告升级为增量小节 |
| 5 | 决定写入已有铁律 3 护栏（锁内快照 items 档位比对） | routes_verify.py | 测试钉扩展到全部新写入路径 |
| 6 | pending 来源问题永远没有 claim_id | annotate_review_claims | §3.4 预案已获批 |
| 7 | docx 已有确定性契约（同输入同字节） | M4 契约 + golden 测试 | 报告 golden file 有现成机制 |

## 2. 决定记录结构（当前状态级，v1.1 定稿）

挂在**被决定对象**上（verify.questions[i] / objections.objections[j]）的新键：

```text
decision = {
  "decision_id": "dc-<sha256[:12]>",
  "decision_type": "human_confirm | human_dispute | objection_adopt",   # 本批三类
  "actor": "user",                       # 轻量版只有人；机器裁决不写决定
  "authority": "human",                  # 机器永不冒充人工
  "claim_id": "cl-...",                  # 决定基于哪条主张（空则不写决定/拒绝采纳）
  "claim_content_hash": "cc-...",        # 决定时点的主张内容指纹快照（硬性验收）
  "evidence_ids": ["ev-...", ...],       # 决定时点主张引用的全部合格证据编号（升序）
  "choice": "confirm | dispute | adopted",
  "human_note": "...",                   # 截 300 字（沿用现截断）
  "revised_quote": "...",                # 截 MAX_QUOTE_CHARS（仅 confirm/dispute）
  "decided_at": "<ISO8601>"              # 服务端时钟（2b-③ 钉 2 同款）
}
```

**字段白名单冻结**：上列 12 键（v1.1 增注：`evidence_ids` 从 v1.0 的派生位明确为白名单成员），多退少不补。

### 2.1 decision_id 与真幂等（v1.1 P2 落实）

- **ID 公式**：`"dc-" + sha256(document_version + <US> + claim_id + <US> + decision_type + <US> + choice)[:12]`。decided_at **不入哈希**（时间随记录走，不参与身份）。
- **幂等语义（三种情形明确定义）**：
  1. **完全相同的重复提交**（choice/note/quote 全一致）→ **整条记录字节不变**：decision_id 不变、**decided_at 不刷新**、其余字段原样——「盖章时间不许偷偷变」；
  2. **仅 human_note / revised_quote 变化** → 定义为**说明修订**：覆盖这两个字段并同步刷新 decided_at（说明是决定的组成部分，修订说明=新的说明时点），decision_id/其余字段不变；**修订在响应与报告中如实呈现最新时间，不是静默偷换**；
  3. **choice 变化**（confirm→dispute）→ 新决定：新 decision_id、新 decided_at、记录整体替换。
- 写入实现统一为一个纯函数 `compose_decision(...)`：输入全部上下文，输出完整记录——三条语义在同一函数内判定，杜绝散落实现。

### 2.2 决定后一致性检测（v1.1 P1-1 落地：读路径派生，不写回 store）

决定落档后，**当前主张与当前证据都可能变化**（规则包更新、票据降级、文本修改）。读路径（get_review / pack_verify 的标注出口）对带 decision 的对象做检测，产出**派生字段** `decision.consistency`（响应层即时计算，**store 不新增任何写入**）：

```text
consistency: "consistent" | "claim_drift" | "evidence_broken"
```

| 检测 | 判定 | 中文文案（报告/API 降级显示） |
|---|---|---|
| 读时派生该对象当前 claim_id ≠ decision.claim_id（主证据变了导致主张键变更） | `claim_drift` | 「**主张内容已变化，当前内容与决定时点不一致**」 |
| claim_id 相同但读时派生的 claim_content_hash ≠ decision.claim_content_hash | `claim_drift` | 同上 |
| decision.evidence_ids 中有编号在当前对象的合格证据（evidence_refs + 合格反证票）中找不到 | `evidence_broken` | 「**决定引用的证据票据已失效或无法定位**」 |
| 两者皆中 | 按 claim_drift > evidence_broken 取最重，报告可并列展示两条 | 两条都显示 |
| 皆无 | `consistent` | 正常显示 |

**三条铁律**：不一致时**不删除决定、不静默改写 decision、不冒充已核实**——
`claim_drift` / `evidence_broken` 的对象在报告与 API 中**禁止出现「已核实/已核验」字样**，
一律替换为对应降级文案。检测逻辑与标注同出口（annotate_review_claims 同一遍），单一实现。

## 3. 写入路径（四路，全部同一把锁）

### 3.1 confirm / dispute（routes_verify.py confirm_question）

锁内顺序：
1. `apply_confirmation(...)` 改问题状态（现状不动）；
2. **写时派生**：对 mini row 跑 `annotate_review_claims`（与 pack_verify 同一函数同一公式）；
3. 取该问题的 claim_id / claim_content_hash / evidence_refs；
4. claim_id 非空 → `compose_decision` 组装并挂载；**claim_id 为空 → 不写 decision**（状态照常更新，功能不被账本掐断，§3.4）；
5. Design B 快照比对（现状）→ store.update → _merge_evidence_index。

### 3.2 recheck（Q3 已裁决：同意）

再核是**系统动作，不冒充人工决定**——recheck 不写 decision；对象上已有 decision **原样保留**；last_recheck 照旧。

### 3.3 objection adopt（v1.1 P1-2 落地：顺序反转 + 前置拒绝）

锁内顺序**改为**：
1. **先写时派生**该异议的 claim_id（annotate 同一公式）；
2. **claim_id 为空 → 直接拒绝采纳**：HTTP 422，`adopted` **不改变**，提示「该异议无有效主张编号（证据不合格），不能采纳为正式决定」——**绝不留下「页面已采纳、档案无决定」的假状态**；
3. claim_id 非空 → 翻 adopted 键（现状）→ `compose_decision`（decision_type=objection_adopt）挂载 → Design B 快照比对（现状）→ update。

### 3.4 无编号主张的人工确认（Q1 已裁决：同意预案）

pending 来源问题确认时：状态照常更新、不写 decision；**报告必须写「已人工确认（该问题无主张编号，未纳入决定链）」**——功能不受影响、账目不说谎。

### 3.5 API 形状（字段先到，前端不动）

- `ConfirmQuestionInfo` / API `Objection` 增 `decision: Optional[DecisionInfo] = None`；
- 新 schema `DecisionInfo`：§2 的 12 键 + **派生字段 `consistency`**（store 不存该键，响应层组装）；
- 旧行无 decision 键 → 默认 None，响应除新增键外逐字节一致。

## 4. docx 报告升级（§4.4 轻量版）

### 4.1 逐条明细节增量

每条需关注/未找到项在「原文摘句」后追加：
- **主张编号行**：`主张编号：cl-xxxx`；无编号 → `主张编号：未编号（证据不合格，未纳入证据链）`；
- **证据状态行**：verified→「摘句已核验定位」；ambiguous→「摘句已定位（多处出现，取首处）」；missing/unverified→「**未能定位到原文**（该条结论未获原文支撑，请人工核查）」；无票据 →「无证据票据」。**红线：不得把「未定位」写成「已核实」**；
- **决定一致性降级（v1.1）**：consistency=claim_drift → 加一行「主张内容已变化，当前内容与决定时点不一致」；evidence_broken → 加一行「决定引用的证据票据已失效或无法定位」——降级状态下该条**禁止出现「已核验」字样**；
- pending 类已确认问题 → 「已人工确认（该问题无主张编号，未纳入决定链）」；
- 反证状态（受理异议）：absent/present/missing 四态中文直陈。

### 4.2 新增「四、人工决定」节（有决定才出现；Q4 已裁决：同意省略空节）

表格：对象 | 决定 | 基于主张 | 内容指纹（cc- 尾 6 位）| 一致性（一致/内容已变化/证据失效）| 决定时间。

### 4.3 四件配套（缺一不验收）

1. 旧报告回归：无 claim/无 decision 旧行 → 报告字节与今天一致；
2. 新报告 golden file：带主张+决定+反证+降级场景的固定样例；
3. 字段缺失降级显示（§4.1 全部「说人话」分支）；
4. 文案九哥把关。

## 5. 不变式与红线

1. **铁律 3 扩展**：所有 decision 写入路径断言 items 档位逐字节不变（四路全钉）；
2. **无 claim 不写决定**（confirm 侧）/ **无 claim 拒绝采纳**（adopt 侧，P1-2）；**有 claim 必带指纹快照**（变异验证：删快照写入 → 红）；
3. **真幂等**（P2）：完全相同重复提交 → 记录字节稳定、decided_at 不刷新；说明修订 → 仅 note/quote/decided_at 变、id 不变；choice 变 → 新 id（§2.1 三条语义全测试钉）；
4. **一致性检测**（P1-1）：漂移/失效不删决定、不静默改写、不冒充已核实；检测与标注同函数同出口；
5. **服务端时钟**：decided_at 服务端产生；
6. **旧行兼容**：无 decision 键读路径零改动；报告逐字节回归；
7. **归一化不突变 store**：写时派生与一致性检测都只读计算；consistency 只在响应层组装，store 不新增冗余键。

## 6. 测试矩阵（v1.1 扩充）

| # | 场景 | 断言 |
|---|---|---|
| T-A1 | confirm（有编号） | decision 12 键齐全；claim_id/指纹/证据集与读路径派生一致；consistency=consistent |
| T-A2 | dispute 改选 | 新 decision_id、记录替换不残留（§2.1-3） |
| T-A3 | pending 问题确认 | 状态照常更新、无 decision、响应/报告如实标注（Q1） |
| **T-A4** | **决定后主张内容变化**（改 note 再读） | consistency=claim_drift；API/报告出现「主张内容已变化…」；决定不被删除或改写；**无「已核验」字样** |
| **T-A5** | **决定后证据失效**（票据降级 missing 再读） | consistency=evidence_broken；「决定引用的证据票据已失效或无法定位」；同上三铁律 |
| **T-A6** | **真幂等** | 完全相同重复提交 → decision 整条字节稳定 + decided_at 不变；仅 note 变 → note/decided_at 更新、id 不变；choice 变 → 新 id |
| T-B1 | adopt（有编号） | decision_type=objection_adopt、证据集含 primary+counter |
| **T-B2** | **adopt（无编号）** | 拒绝 + adopted 仍 False + store 无假状态 + 提示文案（P1-2） |
| T-C1 | 报告-有决定 | golden file：主张编号/证据状态/决定节/一致性列全出现 |
| T-C2 | 报告-旧行 | 无 claim 无 decision 行 → 报告与今天逐字节一致 |
| T-C3 | 报告-降级 | 未定位/无票据/未编号/漂移/失效各分支说人话不冒充 |
| T-D1 | API 往返 | DecisionInfo（含 consistency）不被剥；旧行响应除新增键外逐字节一致 |
| 变异 | 删指纹快照写入 / 删一致性检测 / 删 adopt 前置校验 / 删锁 | 全部必须红；恢复回绿（实际执行） |

## 7. 裁决记录（审计 2026-09-27，Q1-Q4 全部落定）

| # | 问题 | 裁决 |
|---|---|---|
| Q1 | pending 无 claim_id 不写决定 | **同意**——状态可更新，报告必须写「已人工确认，未纳入决定链」（§3.4/§4.1） |
| Q2 | decision_id 公式 | **基本同意**——补齐 §2.1 真幂等三语义后通过 |
| Q3 | recheck 不产生人工决定 | **同意**——系统动作不冒充人工；**旧决定必须保留**（§3.2） |
| Q4 | 无决定省略报告节 | **同意**——不输出空章节（§4.2） |

## 8. 对账表（本稿 ↔ 上位依据）

| 上位要求 | 本稿落点 |
|---|---|
| roadmap 6.1.8 四件事 | §0 逐一对应 |
| v1.3 §4.7 结构+存放形态修订 | §2（当前状态记录、12 键、替换语义） |
| v1.3 硬验收：claim_content_hash 快照 | §2/§5-2/§6 变异 |
| v1.3 §4.4 docx 四件配套+未定位红线 | §4.3/§4.1 |
| 治理冻结（citation_edges 等不做） | §0 不做清单 |
| 外审 2026-09-27「不得写 Ask 生产验证完成」 | 卷首诚实声明 |
| 审计 v1.0 退回 P1-1/P1-2/P2 | §2.2/§3.3/§2.1 + §6 T-A4/A5/A6/B2 |

## 9. v1.0 → v1.1 修订记录（审计退回三项逐一闭环）

| 审计要求 | 落实 |
|---|---|
| P1-1 决定后主张/证据变化的检测与降级语义 | 新增 §2.2 一致性检测（claim_drift/evidence_broken + 三铁律）+ §4.1 报告降级文案 + DecisionInfo.consistency 派生字段 + T-A4/T-A5 |
| P1-2 无 claim_id 异议不得留下 adopted=true | §3.3 顺序反转：先派生后翻键，无编号直接 422 拒绝 + T-B2 |
| P2 幂等不能只保证编号 | §2.1 三语义明确定义（完全重复字节稳定/说明修订/id 不变或新 id）+ T-A6 |
