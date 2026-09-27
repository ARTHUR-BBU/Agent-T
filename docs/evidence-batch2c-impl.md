# 证据法批 2c 专项实现稿（轻量版）：决定引用主张 + 报告可追溯可读

> 状态：**v1.4 修订稿**（2026-09-27，按审计终审意见全文重写；v1.3 判词「报告绕过
> 流水线已补，尚不能终审放行——2 P1 + 2 钉」：①决定记录在 API 的挂载位置未写死
> ②「旧报告逐字节不变」与「新报告增主张信息」互斥 ③build_report_docx 双参迁移
> 未列调用点 ④require_done_row 未分类。全部补齐，对账见 §9）。
> Q1-Q4 审计多轮均裁决同意（§7）。
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
| 1 | confirm/reverify 只改问题级状态字段，无决定对象 | verify.py:727 | 决定挂载点 = 对象新键 |
| 2 | claim 派生读路径现算，store 不存；必须先 normalize 再 annotate | evidence.py | 写读共用流水线（§2.2） |
| 3 | adopt 只翻 `adopted` 布尔键；响应从未标注 | routes_objection.py:41-57 | 前置拒绝 + 响应走流水线（§3.3） |
| 4 | 报告入口 store.get 原件直进 build_report_docx | routes.py:456 | §4.0 入口改造 |
| 5 | **build_report_docx 全部调用点：生产 1 处（routes.py:456）+ 测试约 40 处，全部单参**（v1.4 钉 1 实证） | app/services/report.py:55 | 签名 `warnings=None` 默认值 + 调用点迁移清单（§4.0） |
| 6 | **API 两容器当前均无 decision 字段**（ConfirmQuestionInfo / Objection） | schemas.py:217/296 | v1.4 P1-A：挂载位置写死（§3.5） |
| 7 | 旧行读路径会**现算** claim_id（2b-② 既定行为） | annotate_review_claims | v1.4 P1-B：旧报告字节回归红线废除，改确定性+golden（§4.3） |
| 8 | 决定写入已有铁律 3 护栏 | routes_verify.py | 测试钉扩展到全部写入路径 |
| 9 | pending 来源问题永远没有 claim_id | annotate_review_claims | §3.4 预案已获批 |
| 10 | docx 确定性契约（同输入同字节） | M4 契约 + test_m4 b1==b2 | 契约保持，golden 版本升级 |
| 11 | 异议 evidence_refs 三关系并存 | annotate objection 分支 | T-B1 全覆盖 |
| 12 | claim 内容指纹白名单不含 decision/evidence 字段 | _CONTENT_FIELD_ORDER | 指纹免疫（§5-9+T-D3） |

## 2. 决定记录结构与统一派生

### 2.0 决定对象（store 内 11 键，白名单冻结）

```text
decision = {
  "decision_id": "dc-<sha256[:12]>",
  "decision_type": "human_confirm | human_dispute | objection_adopt",
  "actor": "user",
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

**字段口径（全文唯一）**：store 内 decision = **11 键**；API `DecisionInfo` = **13 键**（11 + 派生 consistency + consistency_reasons，两派生键永不写回 store）。

### 2.1 decision_id 与真幂等（已裁决同意）

- ID 公式：`"dc-" + sha256(document_version + <US> + claim_id + <US> + decision_type + <US> + choice)[:12]`；decided_at 不入哈希。
- 三语义：①完全相同重复提交 → 整条字节稳定、decided_at 不刷新；②仅 note/quote 变 → 说明修订（覆盖+刷新 decided_at，如实呈现），id 不变；③choice 变 → 新 id、整体替换。收口纯函数 `compose_decision`。

### 2.2 唯一权威流水线 `derive_claims_view`

```text
derive_claims_view(mini_row: dict) -> tuple[dict, list]:
    1. copy.deepcopy(store 行片段)
    2. normalize_review_evidence(copy, warnings)   # 旧票据补定位/降级/清 ID
    3. annotate_review_claims(normalized)          # claim_id / claim_content_hash / evidence_refs
    4. 一致性检测（对带 decision 的对象）→ 组装 consistency / consistency_reasons
    → 返回 (派生视图, warnings)
```

- 写路径（confirm/dispute/adopt）与全部主张消费出口共用；禁止跳 normalize、禁止检测另起炉灶（T-D2 + 变异）。

### 2.3 决定后一致性检测（读路径派生，不写回 store）

```text
consistency:         "consistent" | "degraded"
consistency_reasons: ["claim_drift", "evidence_broken"]   # 固定排序，可并存
```

| 检测 | reasons | 中文文案 |
|---|---|---|
| 当前 claim_id ≠ decision.claim_id，或指纹 ≠ 快照 | claim_drift | 「主张身份或内容已变化，当前主张与决定时点不一致」 |
| decision.evidence_ids 有编号不在当前合格证据中 | evidence_broken | 「决定引用的证据票据已失效或无法定位」 |
| 并存 | 两项全留（固定排序） | 两条并列显示 |
| 皆无 | 空 | consistent |

**三铁律**：不删除决定、不静默改写、degraded 态禁止「已核实/已核验」字样。检测与标注同出口单一实现。

### 2.4 evidence_ids 口径

决定时点该对象**全部合格 evidence_refs 去重升序**——primary / rebuts / counter 全覆盖。

### 2.5 全部档案出口处置矩阵（v1.4 钉 2 补分类，穷举 store.get 消费者）

| # | 出口 | 现状 | 2c 处置 |
|---|---|---|---|
| ① | GET /api/review | 已走流水线 | ✅ 增 decision/consistency |
| ② | verify 三端点响应（pack_verify） | 已走流水线 | ✅ 增 decision/consistency |
| ③ | GET /report（routes.py:445-456） | 原件直进报告 | 🔴 §4.0 改走流水线 |
| ④ | adopt 响应（routes_objection.py:57） | 原件直组、从未标注 | ✅ 改走流水线（T-B3） |
| ⑤ | adopt/confirm/reverify 锁内写入 | 原件读改 | ✅ §3 + Design B 快照 |
| ⑥ | _merge_evidence_index | 已走 normalize+rebuild | ✅ 无需改 |
| ⑦ | POST /ask | 原件读 item quote/note/status | ⚪ 不接——消费内嵌原文事实源，非主张消费者（判据防误改） |
| ⑧ | trigger_verify 规则再核输入 | 原件读 items/quality | ⚪ 不接——规则引擎消费规则输入与档位，档位永以 store 原件为准 |
| ⑨ | **require_done_row（deps.py:14）** | store.get + done 状态检查 | ⚪ **不接流水线——它是权限/状态门禁（404 与 done 校验），不消费主张**（v1.4 钉 2 分类）；门禁后各出口按本矩阵各行处置 |

**判据一句话**：消费「主张/证据身份」的出口必须走流水线；消费「档位或内嵌原文事实源」或仅做「权限/状态门禁」的出口保持原件。

## 3. 写入路径（四路，全部同一把锁、同一条流水线 §2.2）

### 3.1 confirm / dispute（routes_verify.py confirm_question）

锁内：apply_confirmation（现状）→ `derive_claims_view` → 取问题 claim 三元组 → 非空则 `compose_decision` 挂载（空则不写，§3.4）→ Design B 快照比对（现状）→ store.update → _merge_evidence_index。

### 3.2 recheck（Q3 已裁决同意）

系统动作不冒充人工：不写 decision；已有 decision 原样保留；last_recheck 照旧。

### 3.3 objection adopt（先拒绝再改状态）

锁内：`derive_claims_view` 派生 claim_id → 为空 **422 拒绝**（adopted 不变 + 提示文案）→ 非空翻 adopted → `compose_decision` 挂载 → Design B 快照 → update。

### 3.4 无编号主张的人工确认（Q1 已裁决同意）

pending 问题确认：状态照常更新、不写 decision；报告写「已人工确认（该问题无主张编号，未纳入决定链）」。

### 3.5 API 挂载位置（v1.4 P1-A 写死，防 pydantic 静默剥字）

```text
app/api/schemas.py:
  class DecisionInfo(BaseModel):        # 13 键，见 §2.0 口径
      decision_id / decision_type / actor / authority / claim_id /
      claim_content_hash / evidence_ids / choice / human_note /
      revised_quote / decided_at                        # 以上 11 = store 原样
      consistency: str = "consistent"                   # 派生
      consistency_reasons: list[str] = []               # 派生

  class ConfirmQuestionInfo(...):       # schemas.py:217
      decision: Optional[DecisionInfo] = None           # ← 挂载点①

  class Objection(...):                 # schemas.py:296
      decision: Optional[DecisionInfo] = None           # ← 挂载点②
```

- **两容器都必须支持**；显式声明字段，pydantic 不得静默剥（批 1 教训）；
- store 内决定挂在**对象上的 `decision` 键**（verify.questions[i].decision / objections.objections[j].decision），与 API 同形；
- 旧档案无 decision → API 返回 `null`；
- **T-D1 钉 verify 响应、T-B3 钉 adopt 响应**：13 字段逐一断言往返存活，任何一键被剥即红。

## 4. docx 报告升级（v1.4 P1-B 重定义回归口径）

### 4.0 报告入口改造

```text
GET /api/review/{id}/report：
    row = store.get(review_id)                      # 404/TTL 语义照旧
    view, warnings = derive_claims_view(row)
    data = build_report_docx(view, warnings)        # 新签名
```

**签名与调用点迁移（v1.4 钉 1）**：`build_report_docx(view, warnings=None)`——
- 生产调用点 1 处：routes.py:456（唯一必须迁移的调用点）；
- 测试调用约 40 处（test_report / test_m4_xiaozhiniang / test_legacy_* / test_audit_fixes / test_credibility_arch_batch3）：`warnings` 缺省 None 合法，单参调用继续可跑；**断言内容受版式影响的测试随 golden 更新逐一修订**（§4.3-2）。

### 4.1 逐条明细节增量

每条需关注/未找到项在「原文摘句」后追加：
- 主张编号行：`主张编号：cl-xxxx`；无编号 → `主张编号：未编号（证据不合格，未纳入证据链）`；
- 证据状态行：verified→「摘句已核验定位」；ambiguous→「摘句已定位（多处出现，取首处）」；missing/unverified→「**未能定位到原文**（该条结论未获原文支撑，请人工核查）」；无票据→「无证据票据」；
- 决定一致性降级：degraded 按 reasons 逐条显示（可并列），降级态禁止「已核验」字样；
- pending 类已确认问题 → 「已人工确认（该问题无主张编号，未纳入决定链）」；
- 反证状态（受理异议）：四态中文直陈。

### 4.2 新增「四、人工决定」节（有决定才出现；Q4 同意省略空节）

表格：对象 | 决定 | 基于主张 | 内容指纹（尾 6 位）| 一致性（可并列）| 决定时间。

### 4.3 报告回归口径（v1.4 P1-B：二选一已定，选方案 1 并写死）

**方案 1（采纳）**：旧档案首次导出**同样包含派生主张信息**（读时现算 claim 是 2b-② 既定行为，报告如实呈现派生视图）。「与今天的旧版报告逐字节一致」红线**废除**，代之以三条仍然坚硬的口径：
1. **确定性契约保持**：同一档案两次导出字节一致（test_m4 b1==b2 机制不变）；
2. **版式 v2 golden 钉死**：新增/更新的固定样例（含旧档案带派生编号、带决定、带降级、带警告各场景）全部落 golden 文件，版式再变必须显式改 golden；
3. **存量报告测试修订清单**：test_report.py / test_m4_xiaozhiniang.py / test_legacy_cleanup.py / test_legacy_xiaozhiniang.py / test_audit_fixes.py / test_credibility_arch_batch3.py 中受新增行影响的断言，随 v2 golden 逐一修订（修订本身进 PR diff，接受审计）。

**配套公示**：报告版式升级（新增主张编号/证据状态/决定节）属用户可见变化，合并后成长日记向用户说明。

**配套四件（原 §4.3 保留，口径随上）**：①旧行导出 = 派生视图渲染的**确定性**回归（不再断言与旧版字节一致）；②v2 golden（含降级/决定/警告场景）；③字段缺失降级显示；④文案九哥把关。

## 5. 不变式与红线

1. 铁律 3 扩展：全部 decision 写入路径断言 items 档位逐字节不变；
2. 无 claim 不写决定（confirm）/ 无 claim 拒绝采纳（adopt）；有 claim 必带指纹快照（变异必红）；
3. 真幂等三语义全测试钉；
4. 写读同流水线 + 报告出口同样接通；变异：写路径跳 normalize / 报告绕过视图 → 必红；
5. 一致性 degraded 三铁律；reasons 并存固定排序；
6. 服务端时钟；
7. **旧行 API 兼容**：无 decision 键读路径零改动；响应除新增键外逐字节一致（报告按 §4.3 新口径）；
8. 归一化不突变 store；consistency 两键只在响应层组装；
9. 指纹免疫：decision 不入内容白名单（T-D3 钉死）；
10. 原文摘句保真：报告摘句仍取内嵌原文，零清洗零派生改写。

## 6. 测试矩阵（v1.4 按新口径修正）

| # | 场景 | 断言 |
|---|---|---|
| T-A1 | confirm（有编号） | decision 11 键齐全；三元组与流水线一致；consistent |
| T-A2 | dispute 改选 | 新 id、替换不残留 |
| T-A3 | pending 确认 | 状态更新、无 decision、如实标注 |
| T-A4 | 决定后指纹变化（verify: question/title；objection: legal_reasoning/proposal/stance_check） | reasons 含 claim_drift；文案；三铁律 |
| T-A5 | 决定后证据失效 | reasons 含 evidence_broken |
| T-A5b | 漂移+失效并存 | 两项并存固定排序；报告两条并列 |
| T-A6 | 真幂等 | 三语义全钉 |
| T-B1 | adopt（有编号） | objection_adopt；evidence_ids=全部合格 refs 去重升序 |
| T-B2 | adopt（无编号） | 422 拒绝 + adopted 不变 + 无假状态 |
| T-B3 | **adopt 响应挂载** | ObjectionInfo.decision 13 字段逐一存活（P1-A 挂载点②） |
| T-C1 | 报告 v2 golden | 主张编号/证据状态/决定节/一致性列全出现 |
| T-C2 | **旧行导出（新口径）** | 旧档案（含带迁移警告行）导出 → 派生主张信息出现（如 cl- 未编号分支）+ **同档案两次导出字节一致**（确定性；不再断言与旧版一致） |
| T-C3 | 报告-缺字段降级 | 未定位/无票据/未编号分支说人话 |
| T-C4 | 带决定导出 | docx 含决定节 |
| T-C5 | 漂移导出 | docx 出现漂移文案 |
| T-C6 | 失效导出 | docx 出现失效文案 |
| T-D1 | **verify 响应挂载** | ConfirmQuestionInfo.decision 13 字段逐一存活（P1-A 挂载点①） |
| T-D2 | 写读同流水线 | 双路径逐字段一致；删 normalize 变异 → red |
| T-D3 | 指纹免疫 | 写 decision 前后 claim_content_hash 不变 |
| 变异 | 删指纹快照 / 删一致性检测 / 删 adopt 前置校验 / 删锁 / 报告绕过视图 | 全部必须红；恢复回绿（实际执行） |

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
| v1.3 总稿 §4.7 结构+存放形态 | §2.0 |
| v1.3 硬验收：指纹快照 | §2.0/§5-2/§6 变异 |
| v1.3 总稿 §4.4 docx 四件配套+未定位红线 | §4.3/§4.1 |
| 治理冻结 | §0 不做清单 |
| 外审「不得写 Ask 生产验证完成」 | 卷首诚实声明 |
| v1.3 终审 2 P1 | §3.5 挂载写死（T-D1/T-B3）/ §4.3 回归口径二选一（方案 1） |
| v1.3 终审 2 钉 | §4.0 签名+调用点清单 / §2.5-⑨ require_done_row 分类 |
| 历史（v1.0→v1.2 全部意见） | §2.2/§2.3/§2.1/§3.3/§6 全量保持 |

## 9. 修订记录

### v1.3 → v1.4（终审 2 P1 + 2 钉）

| 来源 | 落实 |
|---|---|
| P1-A 决定记录挂载位置未写死 | §3.5：DecisionInfo 13 键 + ConfirmQuestionInfo.decision（schemas.py:217）/ Objection.decision（schemas.py:296）双挂载点显式声明；旧档案返回 null；T-D1（verify）/T-B3（adopt）分别钉 13 字段往返存活 |
| P1-B 旧报告字节回归与新增主张信息互斥 | §4.3 明确选方案 1：旧档案导出含派生主张信息；废除「与旧版字节一致」红线，改「确定性契约 + v2 golden + 存量测试修订清单」三件套；合并后日记向用户公示版式升级；v1.3 的「warnings 不渲染」理由同步更正（不再是字节保命，而是设计取舍——警告页面可见） |
| 钉 1 双参迁移 | §4.0：build_report_docx(view, warnings=None)；生产调用点 1 处（routes.py:456）迁移；测试约 40 处单参调用靠默认值兼容，受版式影响断言随 golden 修订（清单：test_report / test_m4_xiaozhiniang / test_legacy_cleanup / test_legacy_xiaozhiniang / test_audit_fixes / test_credibility_arch_batch3） |
| 钉 2 require_done_row 未分类 | §2.5-⑨：权限/状态门禁（404 与 done 校验），不消费主张，不走流水线 |

### 历史版本

v1.1（一致性检测/拒绝采纳/幂等三语义）→ v1.2（写读同流水线/双字段一致性/T-A4 字段/字段口径/T-B1 全关系）→ v1.3（报告出口接流水线/adopt 响应标注/指纹免疫/出口矩阵）。
