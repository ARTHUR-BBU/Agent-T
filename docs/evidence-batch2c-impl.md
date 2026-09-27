# 证据法批 2c 专项实现稿（轻量版）：决定引用主张 + 报告可追溯可读

> 状态：**v1.0 初稿待审**（2026-09-27）。按外审 2026-09-27 生产验收结论放行设计；
> 治理冻结口径（roadmap 6.1.8 + 设计稿 v1.3 战略调整）：**只做最小裁决闭环**，
> citation_edges / decision_history / cited_by 全量账后置冻结，解冻前不得施工。
> 摸底基线：main=5b6e214（生产运行 1249092，两者只差文档/工具）。
> 分隔符记法：`<US>`=U+001F（chr(31) 显式构造，禁字面隐形控制字符）。
>
> **诚实声明（外审 2026-09-27 要求）**：Ask「有效票据」的生产正向验证**尚未完成**
> （坏票拒收已实测，好票入账未在生产出现）——本稿不依赖该能力，相关表述一律按挂账处理。

## 0. 范围一句话与不做清单

**本批做**（roadmap 6.1.8 轻量版四件事）：
1. **人工确认基于哪条主张**——confirm/dispute/adopt 写入「决定记录」，带 claim_id；
2. **决定引用哪条证据**——决定记录带决定时点的证据编号集 + 主张内容指纹快照（防编号不变内容被偷换，v1.3 硬性验收）；
3. **报告可追溯**——docx 从结论追到主张编号、再到原文证据与可定位性；
4. **用户看得懂**——全部中文文案、无编号/无证据时明确说人话，不冒充。

**不做**（后置冻结，roadmap 6.1.8；任何一条出现在实现里即方向偏离）：
- citation_edges / decision_history 只追加账 / cited_by 聚合索引视图；
- 决定历史可回放（轻量版=当前状态记录，新决定替换旧决定）；
- 前端界面改动（决定字段先到，页面展示随报告闭环后的批次）；
- Ask 层决定链 / pending 稳定对象键（本批不阻塞项，见 §3.4）；
- 规则档位任何形式的改写（铁律 3 永久红线）。

## 1. 现状摸底（2026-09-27 @ 5b6e214，全部代码实证）

| # | 事实 | 位置 | 对设计的影响 |
|---|---|---|---|
| 1 | confirm/reverify 只改问题级状态字段（status/human_choice/human_note/revised_quote），**无决定对象** | verify.py:727 apply_confirmation / recheck_question | 决定记录的挂载点 = 问题对象新键 |
| 2 | **claim_id / claim_content_hash 是读路径派生的，store 里不存** | evidence.py annotate_review_claims（get_review/pack_verify 出口调用） | ⚠️ 决定写入在锁内针对 store 原始行——**写路径必须先派生 claim 再落决定**（§3.2），这是本批最重的技术点 |
| 3 | adopt 只翻 `adopted` 布尔键 | routes_objection.py:41-52 | 异议侧决定记录同模式挂载 |
| 4 | docx 报告今天**完全没有**主张编号、证据状态、决定内容；只有规则明细/补盲/政策引用 | report.py（conclusion/attention_table/item_details/blind/policy/appendix） | 报告升级是增量小节，不重构 |
| 5 | 决定写入已有「铁律 3」护栏：confirm/reverify 锁内快照 items 档位并比对，不一致即 500 | routes_verify.py | 铁律 3 测试钉扩展到新写入路径，复用同机制 |
| 6 | pending 来源问题永远没有 claim_id（2b-② 定稿：无稳定对象键不发正式编号） | annotate_review_claims verify 分支 | ⚠️ 引出 Q1 裁决（§3.4）：这类问题的人工确认怎么办 |
| 7 | docx 已有确定性契约（同输入同字节） | M4 确定性契约 + 既有 golden 测试 | 报告升级的 golden file 有现成机制 |

## 2. 决定记录结构（当前状态级，轻量版定稿）

挂在**被决定对象**上（verify.questions[i] / objections.objections[j]）的新键：

```text
decision = {
  "decision_id": "dc-<sha256[:12]>",
  "decision_type": "human_confirm | human_dispute | objection_adopt",   # 本批三类
  "actor": "user",                       # 轻量版只有人；机器裁决（规则引擎）不写决定
  "authority": "human",                  # 审计口径：机器永不冒充人工（v1.3 §4.7 保留）
  "claim_id": "cl-...",                  # 决定基于哪条主张（空则不写决定，§3.4）
  "claim_content_hash": "cc-...",        # 决定时点的主张内容指纹快照（硬性验收，缺此不验收）
  "evidence_ids": ["ev-...", ...],       # 决定时点主张引用的全部合格证据编号（primary+counter，升序）
  "choice": "confirm | dispute | adopted",
  "human_note": "...",                   # 截 300 字（沿用现截断）
  "revised_quote": "...",                # 截 MAX_QUOTE_CHARS（沿用现截断，仅 confirm/dispute）
  "decided_at": "<ISO8601>"              # 服务端时钟（2b-③ 钉 2 同款：不信客户端）
}
```

- **decision_id 幂等键**（Q2 预案）：`sha256(document_version + <US> + claim_id + <US> + decision_type + <US> + choice)[:12]`——同主张同类型同选择的重复提交得同 ID（幂等）；改选（confirm→dispute）得新 ID（新决定）。decided_at **不入哈希**（时间只随记录走，不参与身份）。
- **替换语义**：同一对象再产生新决定时**替换**旧 decision（当前状态记录；完整只追加历史已冻结）。替换发生时旧记录不设坟——这是轻量版的已知与接受的取舍，登记 roadmap 远期。
- **字段白名单冻结**：上列 11 键，多退少不补（2b-③ §3.2 同款纪律）。

## 3. 写入路径（四路，全部同一把锁）

### 3.1 confirm / dispute（routes_verify.py confirm_question）

锁内顺序（扩展现有四步）：
1. `apply_confirmation(...)` 改问题状态（现状不动）；
2. **写时派生**：对 mini row（text/document_version/rule_pack/verify）跑 `annotate_review_claims`（与 pack_verify 同一公式、同一函数——杜绝双实现漂移）；
3. 从派生结果取该问题的 claim_id / claim_content_hash / evidence_refs；
4. claim_id 非空 → 组装 decision 记录挂到问题上；**claim_id 为空 → 不写 decision**（问题状态照常更新，功能不被账本掐断）；
5. Design B 快照比对（现状不动）→ store.update → _merge_evidence_index。

### 3.2 recheck（Q3 预案）

再核不产生**人的**决定（recheck 是系统动作）。轻量版：recheck 不写 decision，但把问题上的**旧 decision 保留原样**；last_recheck 照旧。审计如要求 recheck 也落决定，按 `decision_type` 扩枚举即可（结构不变）。

### 3.3 objection adopt（routes_objection.py）

锁内：adopt 翻键（现状）→ 对 objections 容器跑写时派生取该异议的 claim_id → claim_id 非空则挂 decision（decision_type=objection_adopt, choice="adopted"）→ Design B 快照比对（现状）→ update。**取消采纳（再点一次）**：现状不支持取消，本批不加（不做清单）。

### 3.4 无编号主张的人工确认（Q1，待审计裁决）

pending 来源问题（无合格主证据）claim_id 恒空。两难：
- 按 v1.3「无正式 claim_id 不得写 Decision」→ 这类问题确认后**无决定记录**，报告「人工决定」节看不到它；
- 若为它们造编号 → 违反 2b-② 定稿（pending 无稳定对象键）。

**本稿预案**：不造编号、不写决定；报告对该问题如实显示「已人工确认（该问题无主张编号，未纳入决定链）」——功能不受影响、账目不说谎。**请审计裁决：此预案是否可接受，或要求为 pending 问题设计稳定键（那将扩批）。**

### 3.5 API 形状（字段先到，前端不动——2b-③ 同款纪律）

- `ConfirmQuestionInfo` / API `Objection` 增 `decision: Optional[DecisionInfo] = None`（显式声明防剥字）；
- 新 schema `DecisionInfo` 11 键全声明；
- 旧行无 decision 键 → 默认 None，响应除新增键外逐字节一致。

## 4. docx 报告升级（§4.4 轻量版）

### 4.1 逐条明细节（现有小节内增量）

每条需关注/未找到项在「原文摘句」后追加：
- **主张编号行**：`主张编号：cl-xxxxxxxxxxxx`；无编号 → `主张编号：未编号（证据不合格，未纳入证据链）`——**不得用「暂无」等含糊词冒充**；
- **证据状态行**：verified→「摘句已核验定位」；ambiguous→「摘句已定位（多处出现，取首处）」；missing/unverified→「**未能定位到原文**（该条结论未获原文支撑，请人工核查）」；无票据 → 「无证据票据」。**红线：不得把「未定位」写成「已核实」（§4.4 原文）**；
- 反证状态（受理异议侧）：absent/present/missing 四态中文直陈。

### 4.2 新增「四、人工决定」节（有决定才出现）

表格：对象 | 决定（确认/争议/采纳） | 基于主张 | 内容指纹（cc- 尾 6 位） | 决定时间。无任何决定 → 整节省略（不出「无」空节，报告不膨胀）。

### 4.3 四件配套（§4.4 原文，缺一不验收）

1. 旧报告回归测试：无 claim/无 decision 的旧行 → 报告字节与今天一致（确定性契约复用）；
2. 新报告 golden file：带主张+决定+反证的固定样例；
3. 字段缺失降级显示（上面全部「无 → 说人话」分支）；
4. 文案九哥把关（本稿中文文案全部过九哥）。

## 5. 不变式与红线

1. **铁律 3 扩展**：所有 decision 写入路径断言 items 档位逐字节不变（复用锁内快照机制，测试钉到四路）；
2. **无 claim 不写决定**；**有 claim 必带内容指纹快照**（变异验证：删快照写入 → 测试红）；
3. **幂等**：同主张同类型同选择重复提交 → 同 decision_id、记录不重复；
4. **服务端时钟**：decided_at 服务端产生（钉 2 同款）；
5. **旧行兼容**：无 decision 键的行读路径零改动；报告逐字节回归；
6. **归一化不突变 store**：写时派生只读 store 行做计算，派生结果只进 decision 键，不回写派生字段（claim_id 等仍只在读路径派生——store 不新增冗余）。

## 6. 测试矩阵（v1.0 预案，审计可增删）

| # | 场景 | 断言 |
|---|---|---|
| T-A1 | confirm（有编号问题） | decision 11 键齐全、claim_id/指纹/证据集与读路径派生一致、幂等同 ID |
| T-A2 | dispute + 改选 | confirm→dispute 产生新 decision_id、记录替换不残留 |
| T-A3 | pending 问题确认 | 状态照常更新、无 decision、报告/响应如实标注（Q1 预案） |
| T-B1 | adopt | decision_type=objection_adopt、证据集含 primary+counter |
| T-C1 | 报告-有决定 | golden file：主张编号/证据状态/决定节全出现；「未定位」红线文案正确 |
| T-C2 | 报告-旧行 | 无 claim 无 decision 行 → 报告与今天逐字节一致 |
| T-C3 | 报告-缺字段降级 | 无票据/无编号分支说人话不冒充 |
| T-D1 | API 往返 | DecisionInfo 不被剥；旧行响应除新增键外逐字节一致 |
| 变异 | 删指纹快照写入 / 删锁 | 测试必须红；恢复回绿（实际执行） |

## 7. 请审计裁决的问题

- **Q1**：pending 问题人工确认不写决定、报告如实标注——预案是否可接受？（替代方案=为 pending 设计稳定键，扩批）
- **Q2**：decision_id 幂等键设计（dv+claim_id+type+choice）是否认可？
- **Q3**：recheck 不写决定（仅保留旧决定）是否认可？
- **Q4**：报告「人工决定」节无决定时整节省略（不输出空节）是否认可？

## 8. 对账表（本稿 ↔ 上位依据）

| 上位要求 | 本稿落点 |
|---|---|
| roadmap 6.1.8 四件事 | §0 范围逐一对应 |
| 设计稿 v1.3 §4.7 结构+存放形态修订 | §2（当前状态记录、11 键、替换语义） |
| v1.3 硬验收：claim_content_hash 快照 | §2/§5-2/§6 变异 |
| v1.3 §4.4 docx 四件配套+未定位红线 | §4.3/§4.1 |
| 治理冻结（citation_edges 等不做） | §0 不做清单 |
| 外审 2026-09-27「不得写 Ask 生产验证完成」 | 卷首诚实声明 |
