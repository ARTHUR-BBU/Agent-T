# 证据法批 2c 专项实现稿（轻量版）：决定引用主张 + 报告可追溯可读

> 状态：**v1.3 修订稿**（2026-09-27，按审计终审意见全文重写；v1.2 判词「主体通过，
> 暂不终审放行——1 P1：报告下载入口未走统一派生流水线」。另按审计要求做全面
> 自查，盘出 2 个连带问题一并入稿：adopt 响应未标注、decision 键的指纹免疫声明。
> 全部出口处置矩阵见 §2.5）。Q1-Q4 审计多轮均裁决同意（§7）。
> 治理冻结口径（roadmap 6.1.8 + 设计稿 v1.3 总稿）：**只做最小裁决闭环**，
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
3. **报告可追溯**——docx 从结论追到主张编号、再到原文证据与可定位性；含「决定后主张/证据变化」的如实降级显示；
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
| 1 | confirm/reverify 只改问题级状态字段，无决定对象 | verify.py:727 | 决定记录挂载点 = 对象新键 |
| 2 | **claim_id / claim_content_hash 读路径派生，store 不存**；新票据必须先 normalize 再 annotate | evidence.py | 写读共用同一条流水线（§2.2） |
| 3 | adopt 只翻 `adopted` 布尔键 | routes_objection.py:41-52 | 前置拒绝（§3.3） |
| 4 | **报告入口 `download_report` 直接 `store.get` → `build_report_docx(row)`，零归一化零标注** | routes.py:445 | v1.3 P1 主修（§4） |
| 5 | **adopt 的响应 `ObjectionInfo(**new_obj)` 直接用 store 原件组装，从未标注**（自查发现） | routes_objection.py:57 | 2c 起响应必须走统一视图（§2.5-④） |
| 6 | 决定写入已有铁律 3 护栏 | routes_verify.py | 测试钉扩展到全部写入路径 |
| 7 | pending 来源问题永远没有 claim_id | annotate_review_claims | §3.4 预案已获批 |
| 8 | docx 确定性契约（同输入同字节） | M4 契约 | golden 机制现成 |
| 9 | 异议 evidence_refs 三关系并存：primary/rebuts/counter | annotate objection 分支 | T-B1 全覆盖 |
| 10 | claim 内容指纹白名单（verify=question/title；objection=legal_reasoning/proposal/stance_check）**不含 decision/evidence 等任何票据字段** | _CONTENT_FIELD_ORDER | 决定落档天然不改变主张指纹（§5-9 显式声明+测试钉） |

## 2. 决定记录结构与统一派生

### 2.0 决定对象（store 内 11 键，白名单冻结）

```text
decision = {
  "decision_id": "dc-<sha256[:12]>",
  "decision_type": "human_confirm | human_dispute | objection_adopt",
  "actor": "user",                       # 机器裁决不写决定
  "authority": "human",                  # 机器永不冒充人工
  "claim_id": "cl-...",
  "claim_content_hash": "cc-...",        # 决定时点指纹快照（硬性验收）
  "evidence_ids": ["ev-...", ...],       # 决定时点全部合格引用（去重升序，§2.4）
  "choice": "confirm | dispute | adopted",
  "human_note": "...",                   # 截 300 字
  "revised_quote": "...",                # 截 MAX_QUOTE_CHARS（仅 confirm/dispute）
  "decided_at": "<ISO8601>"              # 服务端时钟
}
```

**字段口径（全文唯一）**：store 内 decision = **11 键**；API `DecisionInfo` = **13 键**（11 + 派生 consistency + consistency_reasons，永不写回 store）。旧行无 decision 键 → API 默认 None。

### 2.1 decision_id 与真幂等（已裁决同意）

- ID 公式：`"dc-" + sha256(document_version + <US> + claim_id + <US> + decision_type + <US> + choice)[:12]`；decided_at 不入哈希。
- 三语义：①完全相同重复提交 → 整条字节稳定、**decided_at 不刷新**；②仅 note/quote 变 → 说明修订（覆盖两字段+刷新 decided_at，如实呈现非偷换），id 不变；③choice 变 → 新 id、整体替换。统一收口纯函数 `compose_decision`。

### 2.2 唯一权威流水线 `derive_claims_view`（写读同源，v1.2 P1-1 保持）

```text
derive_claims_view(mini_row: dict) -> tuple[dict, list]:
    1. copy.deepcopy(store 行片段)
    2. normalize_review_evidence(copy, warnings)   # 旧票据补定位/降级/清 ID；warnings 原样带出
    3. annotate_review_claims(normalized)          # claim_id / claim_content_hash / evidence_refs
    4. 一致性检测（§2.3，对带 decision 的对象）      # 组装 consistency / consistency_reasons
    → 返回 (派生视图, warnings)
```

- **返回 warnings**（v1.3 按审计要求明确）：归一化产生的迁移警告随视图带出——get_review 路径照旧下发给 `claim_migration_warnings`；**报告路径捕获但不渲染**（保旧报告字节回归红线 §4.3-1，警告页面可见）；
- 写路径（confirm/dispute/adopt）与全部读出口共用本函数；**禁止**跳过 normalize 直接 annotate、禁止检测另起炉灶。

### 2.3 决定后一致性检测（读路径派生，不写回 store）

```text
consistency:         "consistent" | "degraded"
consistency_reasons: ["claim_drift", "evidence_broken"]   # 固定排序，可并存
```

| 检测（基于 §2.2 当前派生值） | reasons 追加 | 中文文案 |
|---|---|---|
| 当前 claim_id ≠ decision.claim_id，或 claim_id 同但指纹 ≠ 快照 | claim_drift | 「主张身份或内容已变化，当前主张与决定时点不一致」 |
| decision.evidence_ids 有编号不在当前合格证据中 | evidence_broken | 「决定引用的证据票据已失效或无法定位」 |
| 并存 | 两项全留（固定排序） | 两条并列显示 |
| 皆无 | 空 | consistent |

**三铁律**：不删除决定、不静默改写、degraded 态禁止「已核实/已核验」字样。检测与标注同出口单一实现。

### 2.4 evidence_ids 口径

决定时点该对象**全部合格 evidence_refs 去重升序**——primary / rebuts / counter 全覆盖。

### 2.5 全部档案出口处置矩阵（v1.3 P1 + 自查，穷举 store.get 消费者）

| # | 出口 | 现状 | 2c 处置 |
|---|---|---|---|
| ① | GET /api/review（routes.py:374） | 已走 normalize+annotate+registry | ✅ 增 decision/consistency（经 §2.2 流水线） |
| ② | verify 三端点响应（pack_verify） | 已走 normalize+annotate | ✅ 增 decision/consistency |
| ③ | **GET /review/{id}/report（routes.py:445）** | **store.get 原件直进 build_report_docx** | **🔴 v1.3 主修：改为 `derive_claims_view` 后再进报告构建（§4）** |
| ④ | **adopt 响应（routes_objection.py:57）** | **store 原件直组 ObjectionInfo，从未标注**（自查发现） | ✅ 响应改走 §2.2 流水线组装（连带修复存量未标注） |
| ⑤ | adopt/confirm/reverify 锁内写入 | 原件读改 | ✅ 写路径派生（§3）+ Design B 快照（现状） |
| ⑥ | _merge_evidence_index（routes_verify.py:69） | 已走 normalize+rebuild | ✅ 无需改（decision 11 键不参与索引） |
| ⑦ | POST /ask（routes.py:526/552） | 原件读 item 的 quote/note/status | ⚪ **不接流水线**——Ask 消费的是内嵌原文事实源（quote 保真原则），不消费主张；写明理由防误改 |
| ⑧ | trigger_verify 规则再核输入 | 原件读 items/quality | ⚪ **不接流水线**——规则引擎消费规则输入与档位，不是主张消费者；档位以 store 原件为准（铁律 3 同源） |

**判据一句话**：消费「主张/证据身份」的出口必须走流水线；消费「规则档位或内嵌原文事实源」的出口保持原件（档位永以 store 原件为准，原文摘句永以内嵌为事实源）。

## 3. 写入路径（四路，全部同一把锁、同一条流水线 §2.2）

### 3.1 confirm / dispute（routes_verify.py confirm_question）

锁内：apply_confirmation（现状）→ `derive_claims_view` → 取问题 claim 三元组 → 非空则 `compose_decision` 挂载（空则不写，§3.4）→ Design B 快照比对（现状）→ store.update → _merge_evidence_index。**响应**经 pack_verify（已走流水线）。

### 3.2 recheck（Q3 已裁决同意）

系统动作不冒充人工：不写 decision；已有 decision 原样保留；last_recheck 照旧。

### 3.3 objection adopt（先拒绝再改状态）

锁内：`derive_claims_view` 派生该异议 claim_id → 为空 **422 拒绝**（adopted 不变 + 提示「该异议无有效主张编号（证据不合格），不能采纳为正式决定」）→ 非空翻 adopted 键 → `compose_decision`（objection_adopt）挂载 → Design B 快照 → update。**响应改走 §2.2 流水线组装**（②-④ 连带修复）。

### 3.4 无编号主张的人工确认（Q1 已裁决同意）

pending 问题确认：状态照常更新、不写 decision；报告写「已人工确认（该问题无主张编号，未纳入决定链）」。

## 4. docx 报告升级（v1.3 P1 主修 + §4.4 轻量版）

### 4.0 报告入口改造（本节为 v1.3 核心）

```text
GET /api/review/{id}/report：
    row = store.get(review_id)                     # 404/TTL 语义照旧
    view, warnings = derive_claims_view(row)        # normalize → annotate → consistency
    build_report_docx(view, warnings)               # 新签名：派生视图 + warnings
```

- **事实源双轨制**：原文摘句仍取**内嵌 quote 原文**（保真是证据义务，禁语清洗不碰原文——现状红线不动）；主张编号/证据状态/决定/一致性取**派生视图**；
- warnings 只捕获不渲染（旧报告字节回归红线；迁移警告页面可见）。

### 4.1 逐条明细节增量

每条需关注/未找到项在「原文摘句」后追加：
- 主张编号行：`主张编号：cl-xxxx`；无编号 → `主张编号：未编号（证据不合格，未纳入证据链）`；
- 证据状态行：verified→「摘句已核验定位」；ambiguous→「摘句已定位（多处出现，取首处）」；missing/unverified→「**未能定位到原文**（该条结论未获原文支撑，请人工核查）」；无票据→「无证据票据」。**红线：不得把「未定位」写成「已核实」**；
- 决定一致性降级：degraded 按 reasons 逐条显示（可两条并列），降级态禁止「已核验」字样；
- pending 类已确认问题 → 「已人工确认（该问题无主张编号，未纳入决定链）」；
- 反证状态（受理异议）：四态中文直陈。

### 4.2 新增「四、人工决定」节（有决定才出现；Q4 同意省略空节）

表格：对象 | 决定 | 基于主张 | 内容指纹（尾 6 位）| 一致性（可并列）| 决定时间。

### 4.3 四件配套（缺一不验收）

1. 旧报告回归：无 claim/无 decision 旧行 → 报告字节与今天一致（**含有迁移警告的旧行**——警告不渲染因此字节不变）；
2. 新报告 golden file：带主张+决定+反证+降级固定样例；
3. 字段缺失降级显示；
4. 文案九哥把关。

## 5. 不变式与红线

1. 铁律 3 扩展：全部 decision 写入路径断言 items 档位逐字节不变；
2. 无 claim 不写决定（confirm）/ 无 claim 拒绝采纳（adopt）；有 claim 必带指纹快照（变异必红）；
3. 真幂等三语义全测试钉；
4. **写读同流水线**：一切主张派生必经 `derive_claims_view`；报告出口同样接通（v1.3 P1）；变异：写路径跳过 normalize、或报告路径绕过视图 → 测试必红；
5. 一致性 degraded 三铁律；reasons 可并存、固定排序；
6. 服务端时钟：decided_at 服务端产生；
7. 旧行兼容：无 decision 键读路径零改动；报告逐字节回归；
8. 归一化不突变 store：派生与检测只读；consistency 两键只在响应层组装；
9. **指纹免疫**：decision 键不在任何 claim 内容白名单（verify=question/title；objection=legal_reasoning/proposal/stance_check）——**写决定/改决定不改变主张指纹**（显式测试钉，防「决定污染主张」）；
10. **原文摘句保真**：报告摘句仍取内嵌原文（不做任何清洗/派生改写）。

## 6. 测试矩阵（v1.3 增报告接口级测试）

| # | 场景 | 断言 |
|---|---|---|
| T-A1 | confirm（有编号） | decision 11 键齐全；三元组与流水线一致；consistent |
| T-A2 | dispute 改选 | 新 id、替换不残留 |
| T-A3 | pending 确认 | 状态更新、无 decision、如实标注 |
| T-A4 | 决定后指纹变化（改 verify 的 question/title；异议的 legal_reasoning/proposal/stance_check——**真参与指纹字段**） | reasons 含 claim_drift；中文文案；三铁律 |
| T-A5 | 决定后证据失效 | reasons 含 evidence_broken；同上 |
| T-A5b | 漂移+失效并存 | reasons 两项并存固定排序；报告两条并列 |
| T-A6 | 真幂等 | 三语义全钉 |
| T-B1 | adopt（有编号） | objection_adopt；evidence_ids=全部合格 refs 去重升序（三关系全覆盖） |
| T-B2 | adopt（无编号） | 422 拒绝 + adopted 不变 + 无假状态 + 文案 |
| **T-B3** | **adopt 响应标注**（自查项） | 响应 ObjectionInfo 含派生 claim/consistency 字段（不再是无标注原件） |
| T-C1 | 报告 golden | 主张编号/证据状态/决定节/一致性列全出现 |
| **T-C2** | **旧行导出回归** | 无 claim 无 decision 行（**含带迁移警告的旧行**）→ 报告与今天逐字节一致 |
| T-C3 | 报告-缺字段降级 | 未定位/无票据/未编号各分支说人话不冒充 |
| **T-C4** | **带决定导出**（接口级） | GET report → docx 含决定节与主张编号 |
| **T-C5** | **漂移导出**（接口级） | 决定后改内容再导出 → docx 出现「主张身份或内容已变化…」 |
| **T-C6** | **证据失效导出**（接口级） | 票据降级再导出 → docx 出现「决定引用的证据票据已失效或无法定位」 |
| T-D1 | API 往返 | DecisionInfo 13 键不被剥；旧行响应除新增键外逐字节一致 |
| T-D2 | 写读同流水线 | 写入所得与读派生逐字段一致；删 normalize 变异 → red |
| **T-D3** | **指纹免疫**（自查项） | 写 decision 前后对象 claim_content_hash 不变（decision 键不入白名单） |
| 变异 | 删指纹快照 / 删一致性检测 / 删 adopt 前置校验 / 删锁 / **报告路径绕过视图** | 全部必须红；恢复回绿（实际执行） |

## 7. 裁决记录（审计 2026-09-27，多轮均同意）

| # | 问题 | 裁决 |
|---|---|---|
| Q1 | pending 无 claim 不写决定 | 同意——报告写「已人工确认，未纳入决定链」 |
| Q2 | decision_id + 幂等三语义 | 同意 |
| Q3 | recheck 不产生人工决定 | 同意——旧决定保留 |
| Q4 | 无决定省略报告节 | 同意——不输出空章节 |

## 8. 对账表（本稿 ↔ 上位依据）

| 上位要求 | 本稿落点 |
|---|---|
| roadmap 6.1.8 四件事 | §0 逐一对应 |
| v1.3 总稿 §4.7 结构+存放形态 | §2.0（当前状态记录、11 键） |
| v1.3 硬验收：指纹快照 | §2.0/§5-2/§6 变异 |
| v1.3 §4.4 docx 四件配套+未定位红线 | §4.3/§4.1 |
| 治理冻结 | §0 不做清单 |
| 外审「不得写 Ask 生产验证完成」 | 卷首诚实声明 |
| v1.2 终审 P1：报告出口接流水线 | §2.5-③ / §4.0 / §6 T-C4~C6 / 变异「报告绕过视图」 |
| v1.2 复审 2P1+3P2 | §2.2 / §2.3 / §6 T-A4 / §2.0 口径 / §2.4+T-B1 |
| 自查发现（adopt 响应未标注 / 指纹免疫） | §2.5-④+T-B3 / §5-9+T-D3 |

## 9. 修订记录

### v1.2 → v1.3（终审 P1 + 全面自查）

| 来源 | 落实 |
|---|---|
| 审计终审 P1：报告下载必须走统一派生视图 | §2.5-③ / §4.0 入口改造 / §6 T-C4~C6 接口级测试 + 变异「报告绕过视图」；derive_claims_view 返回 warnings 且报告捕获不渲染（§2.2/§4.3-1） |
| 自查①：adopt 响应用 store 原件从未标注 | §2.5-④ 响应改走流水线 + T-B3 |
| 自查②：decision 键可能污染主张指纹 | §5-9 指纹免疫声明 + T-D3（白名单不含决定字段，现状代码已保证，钉死防回归） |
| 自查③（负面清单）：ask/trigger_verify 不接流水线 | §2.5-⑦⑧ 写明判据与理由，防未来误改 |

### 历史版本

v1.1（首轮 2P1+1P2：一致性检测/拒绝采纳/幂等三语义）→ v1.2（复审 2P1+3P2：写读同流水线/双字段一致性/T-A4 字段/字段口径/T-B1 全关系）。
