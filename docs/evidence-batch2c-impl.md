# 证据法批 2c 专项实现稿（轻量版）：决定引用主张 + 报告可追溯可读

> 状态：**v1.2 修订稿**（2026-09-27，按审计复审意见全文重写；v1.1 判词「基本修对，
> 尚不能放行——2 P1 + 3 P2」：①写决定与读决定未走同一证据整理流水线 ②consistency
> 单字段无法同时表达两类问题 ③T-A4 测试字段不参与指纹 ④字段数量口径不一
> ⑤T-B1 证据集未覆盖 rebuts。五项全部补齐，对账见 §9）。
> Q1-Q4 审计两轮均裁决同意（§7）。
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
3. **报告可追溯**——docx 从结论追到主张编号、再到原文证据与可定位性；**含「决定后主张/证据变化」的如实降级显示**；
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
| 2 | **claim_id / claim_content_hash 是读路径派生的，store 里不存**；且**新票据必须先 normalize 再 annotate**（旧票据有补定位/降级/清 ID 三件事） | evidence.py normalize_review_evidence → annotate_review_claims | 写与读**必须共用同一条流水线**（§3.1，v1.2 P1-1） |
| 3 | adopt 只翻 `adopted` 布尔键 | routes_objection.py:41-52 | v1.1 P1-2：采纳前置校验 claim_id |
| 4 | docx 报告今天完全没有主张编号/证据状态/决定内容 | report.py | 报告升级为增量小节 |
| 5 | 决定写入已有铁律 3 护栏（锁内快照 items 档位比对） | routes_verify.py | 测试钉扩展到全部新写入路径 |
| 6 | pending 来源问题永远没有 claim_id | annotate_review_claims | §3.4 预案已获批 |
| 7 | docx 已有确定性契约（同输入同字节） | M4 契约 + golden 测试 | 报告 golden file 有现成机制 |
| 8 | 异议 evidence_refs 三种关系并存：primary / rebuts / counter | annotate_review_claims objection 分支 | T-B1 证据集必须全覆盖（v1.2 P2） |

## 2. 决定记录结构（当前状态级，v1.2 定稿）

挂在**被决定对象**上（verify.questions[i] / objections.objections[j]）的新键：

```text
decision（store 内 11 键，白名单冻结，多退少不补）：
  "decision_id": "dc-<sha256[:12]>",
  "decision_type": "human_confirm | human_dispute | objection_adopt",   # 本批三类
  "actor": "user",                       # 轻量版只有人；机器裁决不写决定
  "authority": "human",                  # 机器永不冒充人工
  "claim_id": "cl-...",                  # 决定基于哪条主张（空则不写决定/拒绝采纳）
  "claim_content_hash": "cc-...",        # 决定时点的主张内容指纹快照（硬性验收）
  "evidence_ids": ["ev-...", ...],       # 决定时点主张引用的全部合格证据编号（去重升序，§2.3）
  "choice": "confirm | dispute | adopted",
  "human_note": "...",                   # 截 300 字（沿用现截断）
  "revised_quote": "...",                # 截 MAX_QUOTE_CHARS（仅 confirm/dispute）
  "decided_at": "<ISO8601>"              # 服务端时钟（2b-③ 钉 2 同款）
```

**字段数量口径（v1.2 统一，杜绝开发误读）**：
- **store 内 decision 对象 = 11 键**（上列，白名单冻结）；
- **API 层 `DecisionInfo` = 13 键** = store 11 键 + 响应层派生 `consistency` + `consistency_reasons`（§2.2）——**派生两键永不写入 store**；
- 除上述外不多不少；旧行无 decision 键 → API 默认 None。

### 2.1 decision_id 与真幂等（审计已裁决同意）

- **ID 公式**：`"dc-" + sha256(document_version + <US> + claim_id + <US> + decision_type + <US> + choice)[:12]`。decided_at **不入哈希**。
- **幂等三语义**：
  1. **完全相同的重复提交**（choice/note/quote 全一致）→ **整条记录字节不变**：decision_id 不变、**decided_at 不刷新**；
  2. **仅 human_note / revised_quote 变化** → **说明修订**：覆盖两字段并同步刷新 decided_at（如实呈现非偷换），decision_id/其余字段不变；
  3. **choice 变化** → 新决定：新 decision_id、新 decided_at、记录整体替换。
- 写入统一收口纯函数 `compose_decision(...)`，三语义同函数判定。

### 2.2 写读同一条流水线（v1.2 P1-1 落地：施工顺序写死）

claim 派生是「normalize → annotate」两步（旧票据要补定位/降级/清 ID，跳过 normalize 会拿旧 ID——**刚写完就被判 drift** 的根源）。**唯一权威流水线函数**：

```text
derive_claims_view(mini_row: dict) -> dict:
    1. copy.deepcopy(store 行片段)
    2. normalize_review_evidence(copy)        # 旧票据重定位/降级/清 ID
    3. annotate_review_claims(normalized)     # claim_id / claim_content_hash / evidence_refs
    4. 返回派生视图
```

- **写路径**（confirm/dispute/adopt）：锁内 `derive_claims_view` → 找目标对象 → 取 claim 三元组 → `compose_decision`；
- **读路径**（一致性检测）：get_review / pack_verify 出口的现有标注链**就是这条流水线**——检测复用其结果，不另起炉灶；
- **禁止**任何写入路径跳过 normalize 直接 annotate；**禁止**检测用与写入不同的派生实现。测试钉：T-D2 双路径同源（§6）。

### 2.3 决定后一致性检测（读路径派生，不写回 store）

```text
consistency:        "consistent" | "degraded"          # 单一状态位
consistency_reasons: ["claim_drift", "evidence_broken"]  # 固定排序的明细（可同时含两者）
```

| 检测（基于 §2.2 流水线当前派生值） | reasons 追加 | 中文文案（报告/API 降级显示） |
|---|---|---|
| 当前 claim_id ≠ decision.claim_id，**或** claim_id 同但当前 claim_content_hash ≠ 快照 | `claim_drift` | 「**主张身份或内容已变化，当前主张与决定时点不一致**」（规则包版本变化会换编号而文字未必变——文案不预设是哪种） |
| decision.evidence_ids 中有编号不在当前对象合格证据（evidence_refs 全部关系 + 合格反证票）中 | `evidence_broken` | 「**决定引用的证据票据已失效或无法定位**」 |
| 两者皆中 | reasons 两项并存 | **两条提示都显示**（不再只留最重一条） |
| 皆无 | reasons 空 | consistency=consistent，正常显示 |

**三铁律**：不一致时**不删除决定、不静默改写 decision、不冒充已核实**——degraded 对象在报告与 API 中**禁止出现「已核实/已核验」字样**。检测逻辑与标注同出口，单一实现。

### 2.4 evidence_ids 的口径（v1.2 P2 落实）

`decision.evidence_ids` = 决定时点该对象**全部合格 evidence_refs 的 evidence_id 去重升序**——
primary / rebuts / counter **三种关系全覆盖**，不遗漏 rebuts（「我反对的那条规则项的证据」也是决定依据的一部分）。

## 3. 写入路径（四路，全部同一把锁、同一条流水线 §2.2）

### 3.1 confirm / dispute（routes_verify.py confirm_question）

锁内顺序：
1. `apply_confirmation(...)` 改问题状态（现状不动）；
2. `derive_claims_view(mini_row)`（**含 normalize，§2.2**）；
3. 取该问题 claim_id / claim_content_hash / evidence_refs；
4. claim_id 非空 → `compose_decision` 组装挂载；为空 → 不写 decision（状态照常更新，§3.4）；
5. Design B 快照比对（现状）→ store.update → _merge_evidence_index。

### 3.2 recheck（Q3 已裁决：同意）

再核是**系统动作，不冒充人工决定**——不写 decision；对象上已有 decision **原样保留**；last_recheck 照旧。

### 3.3 objection adopt（v1.1 P1-2 保持：先拒绝再改状态）

锁内顺序：
1. `derive_claims_view`（**含 normalize**）派生该异议 claim_id；
2. **为空 → 422 拒绝采纳**：`adopted` 不改变，提示「该异议无有效主张编号（证据不合格），不能采纳为正式决定」；
3. 非空 → 翻 adopted 键 → `compose_decision`（objection_adopt）挂载 → Design B 快照比对 → update。

### 3.4 无编号主张的人工确认（Q1 已裁决：同意预案）

pending 来源问题确认时：状态照常更新、不写 decision；**报告必须写「已人工确认（该问题无主张编号，未纳入决定链）」**。

### 3.5 API 形状（字段先到，前端不动）

- `ConfirmQuestionInfo` / API `Objection` 增 `decision: Optional[DecisionInfo] = None`；
- `DecisionInfo` = **13 键**（store 11 + 派生 consistency/consistency_reasons，见 §2 口径）；
- 旧行无 decision 键 → 默认 None，响应除新增键外逐字节一致。

## 4. docx 报告升级（§4.4 轻量版）

### 4.1 逐条明细节增量

每条需关注/未找到项在「原文摘句」后追加：
- **主张编号行**：`主张编号：cl-xxxx`；无编号 → `主张编号：未编号（证据不合格，未纳入证据链）`；
- **证据状态行**：verified→「摘句已核验定位」；ambiguous→「摘句已定位（多处出现，取首处）」；missing/unverified→「**未能定位到原文**（该条结论未获原文支撑，请人工核查）」；无票据 →「无证据票据」。**红线：不得把「未定位」写成「已核实」**；
- **决定一致性降级**：degraded 时按 reasons 逐条显示（可两条并列）——「主张身份或内容已变化，当前主张与决定时点不一致」/「决定引用的证据票据已失效或无法定位」；降级态**禁止出现「已核验」字样**；
- pending 类已确认问题 → 「已人工确认（该问题无主张编号，未纳入决定链）」；
- 反证状态（受理异议）：absent/present/missing 四态中文直陈。

### 4.2 新增「四、人工决定」节（有决定才出现；Q4 已裁决：同意省略空节）

表格：对象 | 决定 | 基于主张 | 内容指纹（cc- 尾 6 位）| 一致性（一致/身份或内容已变化/证据失效，可并列）| 决定时间。

### 4.3 四件配套（缺一不验收）

1. 旧报告回归：无 claim/无 decision 旧行 → 报告字节与今天一致；
2. 新报告 golden file：带主张+决定+反证+降级场景的固定样例；
3. 字段缺失降级显示（§4.1 全部「说人话」分支）；
4. 文案九哥把关。

## 5. 不变式与红线

1. **铁律 3 扩展**：所有 decision 写入路径断言 items 档位逐字节不变（四路全钉）；
2. **无 claim 不写决定**（confirm 侧）/ **无 claim 拒绝采纳**（adopt 侧）；**有 claim 必带指纹快照**（变异：删快照写入 → 红）；
3. **真幂等**：三语义全测试钉（§2.1）；
4. **写读同流水线**（v1.2 P1-1）：一切 claim 派生必经 `derive_claims_view`（normalize → annotate）；变异：写路径跳过 normalize → T-D2 必须红；
5. **一致性检测**：degraded 不删决定、不静默改写、不冒充已核实；reasons 可并存、固定排序；
6. **服务端时钟**：decided_at 服务端产生；
7. **旧行兼容**：无 decision 键读路径零改动；报告逐字节回归；
8. **归一化不突变 store**：派生与检测都只读计算；consistency/consistency_reasons 只在响应层组装。

## 6. 测试矩阵（v1.2 修正）

| # | 场景 | 断言 |
|---|---|---|
| T-A1 | confirm（有编号） | decision 11 键齐全；三元组与流水线派生一致；consistency=consistent |
| T-A2 | dispute 改选 | 新 decision_id、记录替换不残留 |
| T-A3 | pending 问题确认 | 状态照常更新、无 decision、响应/报告如实标注（Q1） |
| T-A4 | **决定后主张指纹变化**（**改 verify 问题的 `question` 或 `title`** / 改异议的 `legal_reasoning`/`proposal`/`stance_check`——**必须改真正参与指纹的字段**，防假绿） | consistency_reasons 含 claim_drift；中文文案出现；决定不被删除或改写；无「已核验」字样 |
| T-A5 | 决定后证据失效（票据降级 missing 再读） | reasons 含 evidence_broken；文案出现；同上三铁律 |
| T-A5b | **漂移+失效并存** | reasons 两项并存且排序固定（claim_drift 在前）；报告两条并列显示 |
| T-A6 | 真幂等 | 完全相同重复提交字节稳定 + decided_at 不变；仅 note 变 → note/decided_at 变、id 不变；choice 变 → 新 id |
| T-B1 | adopt（有编号） | decision_type=objection_adopt；**evidence_ids == 该异议当前全部合格 evidence_refs 去重升序（primary/rebuts/counter 全覆盖）** |
| T-B2 | adopt（无编号） | 422 拒绝 + adopted 仍 False + store 无假状态 + 提示文案 |
| T-C1 | 报告-有决定 | golden file：主张编号/证据状态/决定节/一致性列全出现 |
| T-C2 | 报告-旧行 | 无 claim 无 decision 行 → 报告与今天逐字节一致 |
| T-C3 | 报告-降级 | 未定位/无票据/未编号/漂移/失效各分支说人话不冒充 |
| T-D1 | API 往返 | DecisionInfo 13 键不被剥；旧行响应除新增键外逐字节一致 |
| **T-D2** | **写读同流水线** | 写入所得 decision 与读路径派生逐字段一致；**变异：写路径跳过 normalize → 必须 red**（旧票据场景构造） |
| 变异 | 删指纹快照 / 删一致性检测 / 删 adopt 前置校验 / 删锁 | 全部必须红；恢复回绿（实际执行） |

## 7. 裁决记录（审计 2026-09-27，两轮均同意）

| # | 问题 | 裁决 |
|---|---|---|
| Q1 | pending 无 claim_id 不写决定 | **同意**——状态可更新，报告必须写「已人工确认，未纳入决定链」 |
| Q2 | decision_id 公式 + 幂等三语义 | **同意**（§2.1） |
| Q3 | recheck 不产生人工决定 | **同意**——旧决定必须保留 |
| Q4 | 无决定省略报告节 | **同意**——不输出空章节 |

## 8. 对账表（本稿 ↔ 上位依据）

| 上位要求 | 本稿落点 |
|---|---|
| roadmap 6.1.8 四件事 | §0 逐一对应 |
| v1.3 §4.7 结构+存放形态修订 | §2（当前状态记录、11 键、替换语义） |
| v1.3 硬验收：claim_content_hash 快照 | §2/§5-2/§6 变异 |
| v1.3 §4.4 docx 四件配套+未定位红线 | §4.3/§4.1 |
| 治理冻结（citation_edges 等不做） | §0 不做清单 |
| 外审 2026-09-27「不得写 Ask 生产验证完成」 | 卷首诚实声明 |
| v1.0 退回 P1-1/P1-2/P2 | §2.2（v1.2 重构）/§3.3/§2.1 |
| v1.1 复审 2P1+3P2 | §2.2 流水线 / §2.3 双字段 / §6 T-A4 字段修正 / §2 字段口径 / §2.4+T-B1 全关系覆盖 |

## 9. 修订记录

### v1.1 → v1.2（复审五项闭环）

| 审计要求 | 落实 |
|---|---|
| P1-1 写读同流水线 | §2.2 `derive_claims_view`（deepcopy → normalize → annotate）写死施工顺序；§5-4 + T-D2（含删 normalize 变异） |
| P1-2 consistency 单字段表达不了双问题 | §2.3 改 `consistency(consistent/degraded) + consistency_reasons`（固定排序可并存）；文案改「主张身份或内容已变化…」；T-A5b 并存场景 |
| P2 T-A4 字段不参与指纹 | §6 T-A4 明确改 question/title（verify）与 legal_reasoning/proposal/stance_check（异议） |
| P2 字段数量口径 | §2 统一：store 11 键 / API DecisionInfo 13 键（11+2 派生），全文不再出现第二口径 |
| P2 T-B1 漏 rebuts | §2.4 + §6 T-B1：evidence_ids = 全部合格 evidence_refs 去重升序（primary/rebuts/counter 全覆盖） |

### v1.0 → v1.1（首轮退回三项）

P1-1 决定后变化检测与降级语义（§2.3 前身）/ P1-2 无 claim 拒绝采纳（§3.3）/ P2 幂等三语义（§2.1）。
