# LLM 在 Agent-T 中的定位、权限与分阶段开发规范

> **状态：规范性开发文件（Normative Development Standard）**
> **版本：v1.0**
> **审阅日期：2026-09-18**
> **代码基线：`main@3d9f1db`；PR #57 `docs/judge-counsel-architecture@75d9e3a`**
> **上位文件：[《Agent-T Constitution / Agent-T 宪法》](agent-t-constitution.md)**
> **适用范围：所有 LLM 调用、Prompt、模型路由、Evidence、Objection、Ask、Quality、Verify、Pipeline、状态对象、配置、测试、日志、覆盖率与后续 RAG / Action 设计。**

---

## 0. 文件效力与阅读方式

本文件回答开发组必须共同回答的四个问题：

1. **LLM 在 Agent-T 中是什么，不是什么；**
2. **当前代码已经让 LLM 做了什么，做到什么程度；**
3. **每一个调用节点的输入、输出、权限、门禁和失败语义是什么；**
4. **下一阶段先做什么，达到什么标准才可以宣布完成。**

本文件不是产品愿景稿，也不是把 roadmap 重新抄一遍。本文中的“当前已实现”只以所列代码基线的可执行代码、配置和测试为依据。README、旧 spec 或路线图若与代码事实冲突，应先修正文档；不得用旧文档反推代码已经具备某项能力。

规范冲突时按以下顺序处理：

1. Agent-T Constitution；
2. 本文件定义的 LLM 权限与节点契约；
3. 专项 spec、法务裁定书和 Rule Pack 治理文件；
4. roadmap 与普通说明文档；
5. Prompt 内的自然语言要求。

任何扩大 LLM 权限、改变 Rule / AI / Human 优先级、改变证据真实性或相关性定义、取消人工确认、允许运行时修改规则或自动执行真实法律/财务动作的改动，均属于 **Constitution impact**，不得按普通功能 PR 处理。

---

# 第一部分：定位与权力结构

## 1. LLM 的正式定位

> **LLM 是受治理系统调用的“认知能力”，不是治理系统本身。**

LLM 可以在不同节点临时扮演分类助手、分析员、律师、解释者或草拟者；它在该节点拥有什么角色，取决于：

- 节点在 Pipeline 中的位置；
- 服务端提供的输入上下文；
- 允许调用的工具；
- 标准输出对象；
- 服务端验证门禁；
- 失败语义；
- 是否需要人工确认。

模型在文字中声称自己拥有某种权力，不产生任何工程效力。

## 2. LLM 永久没有的权力

除非未来经过修宪级变更，任何 LLM 调用均不得直接：

- 新建、修改或删除 Rule Pack；
- 输出或覆盖正式四档状态：`通过 / 需关注 / 未找到 / 本类不适用`；
- 修改 `rule_id`、`rule_class`、适用条件或规则优先级；
- 将未核验文本写成正式 Evidence Fact；
- 把“真实但无关”的摘句作为当前争点的证据；
- 把建议改写稿声明为已通过复审；
- 代表用户接受风险、放弃权利或作出法律判断；
- 自动签署、付款、发函、提交审批或修改外部业务数据；
- 因自身输出而扩大下一步工具权限、预算或可见数据范围。

## 3. 当前正式结果与参考结果

| 对象 | 当前权威来源 | 性质 | LLM 能否直接修改 |
|---|---|---|---|
| `items[].status` | `run_checklist()` + Rule Pack | 正式规则结果 | **不能** |
| `items[].rule_id / rule_class` | Rule Pack 与 Checklist Engine | 正式规则元数据 | **不能** |
| `EvidenceRef.verification` | 服务端原文定位与范围校验 | 证据核验状态 | **不能自行声明** |
| `scorecard` | LLM 建议 + 服务端重算、扣分和封顶 | 参考评分 | 可以提议，代码定最终展示值 |
| `blind_candidates` | LLM 候选 + quote / target 校验 | 待确认候选 | 只能新增候选，不能改档位 |
| `quality.observations` | LLM 观察 + 服务端清洗 | 待人工确认的参考观察 | 不能回流 `items` / `scorecard` |
| `objections.objections` | LLM 异议 + 服务端受理门禁 | 律师式异议候选 | 采纳也不能改当次档位 |
| `verify` | 确定性取证、分流与人工确认 | 有界核验卷宗 | 当前不调用 LLM，不能改档位 |
| `ask.answer` | 单次 LLM 回答 + quote 校验 | 用户按需解释 | 不能成为正式结果或自动执行依据 |

## 4. 当前真实 Pipeline

```text
上传
  ├─ 文件限制 / 解析并发门禁
  ├─ LLM Precheck（同步、可降级）
  │    └─ 只决定：继续 / 请用户确认品类 / 不支持提示
  └─ 后台审查
       parse（确定性）
         → checklist（确定性正式四档 + EvidenceRef）
         → model_review（LLM：scorecard + blind candidates）
         → quality（LLM：三维观察 + facts / pending questions）
         → bounded_verify（确定性：取证、分流、人工确认待办）
         → objection（LLM：受限异议候选）
         → fully_complete

结果完成后，用户可另外触发：
  ├─ Ask（LLM：仅对“需关注”项单次追问）
  ├─ Confirm / Reverify（确定性，不调用 LLM）
  └─ Adopt Objection（只标记规则改进提案，不修改正式结果）
```

LangGraph 当前是固定顺序的库内 Pipeline，不是能自行规划、增删节点或无限调用工具的自治 Agent。A6 Verify 当前以确定性程序为主，也不应被描述为“LLM 自主查证”。

---

# 第二部分：当前实现审计结论

## 5. 状态标记

- **已完成（当前范围）**：代码、API / 状态对象和确定性测试均已存在；只表示当前定义范围完成，不等于真实模型质量已获生产证明。
- **部分完成**：已有可运行能力，但产品闭环、生产可观测性、真实模型验收或版本治理仍有明确缺口。
- **未实现**：当前代码没有该能力；不得在 README、演示或开发汇报中描述为现成功能。
- **非 LLM 节点**：明确由确定性程序承担；不得为了“更智能”而默认改成模型调用。

## 6. 能力与成熟度矩阵

| 能力 | 代码状态 | 生产保障状态 | 当前结论 |
|---|---|---|---|
| 多供应商 OpenAI-compatible transport | 已完成（当前范围） | 部分完成 | DeepSeek > 智谱 / GLM > xAI；共享服务端 Key。缺少逐调用审计账本、token / cost / latency、request id、prompt 版本与 per-user credential。 |
| LLM Precheck 合同分类与支持性判断 | 已完成（当前范围） | 部分完成 | 有白名单、一次解析重试、并发闸门、短超时和确认分支；长合同只取约 3000 字头尾，真实模型专项不进 required CI。 |
| Rule Checklist 正式四档 | 已完成 | 已完成（确定性范围） | **不调用 LLM**；是当前唯一正式状态签发者。 |
| Scorecard 参考评分 | 已完成（当前范围） | 部分完成 | 短合同单轮；长合同 map-reduce；分数由代码重算、扣分、封顶。模型真实稳定性与业务校准不进 required CI。 |
| Targeted Blind Spot 补盲 | 已完成（当前范围） | 部分完成 | 只能映射既有目标 item，必须有全文可核 quote；仍不能发现全新规则外争点。 |
| Quality 三维观察 | 已完成（当前范围） | 部分完成 | 完整性 / 一致性 / 影响；quote、clause、禁语、条数有门禁；真实模型召回率与误报率未形成发布阈值。 |
| EvidenceRef 与事实材料 | 已完成（当前范围） | 部分完成 | 有文档指纹、quote、span、clause、verification、source、**全局稳定 `evidence_id`**（2026-09-19，证据法批 1：内容哈希自证、missing 不发 ID、attach 归一化历史票据、读路径全容器归一化——items/blind/quality/facts/objections/verify 六容器 + verify 子路由出口，含问题级 verification 同步与空版本回填）；证据命题、支持/反驳关系与跨文档来源仍缺。 |
| A6 Bounded Verify | 已完成（有界范围） | 已完成（确定性范围） | 当前不调用 LLM；支持疑点收集、条款取证、分流、人工确认、再核和预算。语义分流仍是少量固定规则。 |
| LLM Objection 3.1 / 3.2 | 已完成（当前范围） | 部分完成 | heuristic 误报、existence 漏报；hardline 不送审；五要件受理、adopt 标记已实现。采纳后自动形成版本化 Rule Pack PR 的通道未实现。 |
| 阶段 3.3 对抗测试 | 已完成（mock / corpus 范围） | 部分完成 | 已有 corpus、hardline / existence / heuristic 和范围对抗测试；真实供应商、真实长合同与法律专家验收仍不是 required gate。 |
| Ask 单次追问与建议改写 | 已完成（当前范围） | 部分完成 | 仅 `需关注` item；有条款上下文、问题限长、IP 限频、quote 校验。无会话状态、无每审查调用上限、回答不持久化、无修改后复审。 |
| 修改稿 A/B/C、版本 diff、连带影响与重审 | 未实现 | 未实现 | Ask 的单份“改写稿”不等于修订验证闭环。 |
| 多文档项目空间 / RAG | 未实现 | 未实现 | 无文档权限、版本图、检索索引、证据权重、跨文档引用或缺失材料闭环。 |
| 法律检索、计算器、外部专业工具 | 未实现 | 未实现 | Constitution 中的“专家证人”是治理角色定义，不是现成功能。 |
| 自动执行真实法律 / 财务动作 | 未实现且禁止默认实现 | 未实现 | 未来如需建设，必须先走 Constitution impact 审查。 |

## 7. 不能被混淆的三组概念

### 7.1 “代码已实现”不等于“模型能力已验收”

当前 required CI 主体使用 mock LLM，真实模型语料测试标记为 `live_llm`，无 Key 自动 skip，且 `tests/test_precheck_corpus.py` 被 CI 显式排除。因此可以说“程序门禁与降级路径有回归测试”，不能据此说“某个真实模型已经达到法律审查质量”。

### 7.2 “引用真实”不等于“证据相关”

`quote` 能在全文找到，只证明文字存在。它是否属于当前条款、当前争点、模型实际可见范围，并能支持所提出的主张，仍需范围校验。Objection 已有 `allowed_clause_ids` 相关性门禁；其它节点不得把简单 substring 命中包装成完整证据效力。

### 7.3 “采纳异议”不等于“改判”

当前 `/objections/adopt` 只把合格异议标记为 `adopted=true`，供人复制成规则改进提案。它不修改当次 `items[].status`，也没有自动完成 Rule Pack 版本修订、金标回放和合并流程。

---

# 第三部分：标准对象与共同契约

## 8. 三层对象模型

所有后续实现必须保持以下分层：

```text
Evidence Fact（原文事实）
  document_version + quote + span + clause + verification + source
          ↓ 支持 / 反驳
Claim / Candidate（主张或候选）
  issue_id + proposition + direction + reasoning + evidence_refs
          ↓ 经规则或治理程序处理
Decision / Human Decision（正式结果或人工决定）
  authority + status + rule_version + rationale + audit metadata
```

当前 `EvidenceRef` 已覆盖第一层的基础字段并具备稳定 `evidence_id`（2026-09-19）。仍缺显式 proposition 与「各层引用同一 Evidence 对象」的引用化改造——当前各层仍按各自摘句建票据（可经 evidence_id 关联去重，但尚未消除复制）。进入多轮核验、修订验证或多文档阶段前，必须完成引用化改造，不得继续让各层复制 quote 字符串充当关联键。

## 9. 当前 EvidenceRef 标准

```json
{
  "document_version": "sha256-prefix",
  "quote": "合同原文连续摘录",
  "start": 120,
  "end": 156,
  "clause_id": "c05",
  "verification": "verified | ambiguous | unverified | missing",
  "parse_source": "rules | blind | quality | ask | fact"
}
```

约束：

- `document_version` 必须来自服务端对入库正文计算的指纹；
- `start / end` 必须锚定同一版本正文；
- `clause_id` 应由服务端按坐标派生，不信任模型申报；
- `verification=verified` 只表示定位唯一，不自动代表法律相关性或结论成立；
- 多处命中必须标 `ambiguous`，不得挑一个对结论最有利的位置冒充唯一来源；
- 没有摘句时必须显式 `missing / unverified`，不能省略后被前端当作已核验。

## 10. 所有 LLM 输出的共同要求

每个 LLM 节点必须同时满足：

1. 有独立、版本化的输入和输出 schema；
2. 合同正文始终作为不可信数据处理；
3. 结构解析失败不得把裸文本写入正式对象；
4. 模型提供的 `rule_id / clause_id / status / severity / authority` 不自动可信；
5. 输出进入状态前必须经过服务端白名单、类型、长度、数量、引用与范围校验；
6. 失败必须映射为固定 reason code，供应商错误详情只进受控日志；
7. 可选 AI 节点失败不得抹掉已完成的 Rule Result；
8. coverage 不完整时必须对 API、UI 和后续节点可见；
9. 人工确认、模型意见、正式状态必须写入不同字段；
10. 下游节点不得把上游“参考结果”静默提升为“正式事实”。

---

# 第四部分：各调用节点开发契约

## 11. N1 — LLM Precheck：分类与可审性分诊

| 项目 | 规范 |
|---|---|
| 触发事件 | 用户上传并通过文件类型、大小和解析门禁后，正式审查建档前。 |
| 角色 | 立案 / 分诊助手；判断“是什么合同、是否在支持白名单”。 |
| 当前输入 | 约 3000 字头尾采样、用户选择品类、用户声明立场、支持品类列表。 |
| 标准输出 | `PrecheckResult { detected_type, is_supported, suggested_category, confidence, summary }`。 |
| 程序环境 | 同步请求内通过线程池执行；独立 30 秒默认超时；并发上限 2；计入单次审查 LLM 预算。 |
| 权限 | 可建议品类、触发用户确认、提示不支持；不得写 `items`、不得评价风险、不得静默切换品类。 |
| 验证门禁 | `suggested_category` 白名单；支持性与建议品类自洽；confidence 枚举；字段限长；禁语清洗；结构失败只重试一次。 |
| 失败语义 | `disabled / no_llm_key / busy / llm_error / parse_failed / budget_exceeded` 均 fail-soft，继续用户原选品类；解析本身失败仍由主审查 fail-closed。 |
| 当前日志 / coverage | 记录固定失败原因与异常日志；没有输入覆盖比例、provider / model / latency / token、prompt 版本和 request id。 |
| 当前测试 | mock 单测、分支/API/对抗 corpus；真实 LLM 专项不进 required CI。 |

进一步约束：Precheck 是当前少数可能阻断“立即建档”的 LLM 节点，因此它只能要求用户确认，不能代表用户作最终选择。长合同的头尾采样必须在 UI 或审计数据中视为有限覆盖；在未实现全文分类证据之前，不得把 high confidence 等同于全文审阅。

### N1 验收标准

- 不支持类型不得生成伪专业报告；
- high / medium 不一致必须由用户确认，不能静默换尺子；
- low confidence 不阻断，但必须保留可见的品类存疑；
- 无 Key、超时、并发满、预算尽时规则审查仍可继续；
- 恶意正文不能改输出 schema、支持白名单或系统职责；
- 发布前真实模型金标必须按供应商和模型版本分别计准确率、拒审率与误阻断率。

## 12. N2 — Model Review：Scorecard + Targeted Blind Spot

| 项目 | 规范 |
|---|---|
| 触发事件 | `checklist` 已完成并产生规则 items 后。 |
| 角色 | 参考评分员 + 定向补盲助手；不是法官。 |
| 当前输入 | 合同文本、规则 items、policies、scorecard segments、品类、条款索引；长合同按条款切块。 |
| 标准输出 | 合并 payload：`scorecard { summary, segments, gap_item_ids... }` + `candidates[]`。服务端产出 `ScorecardInfo` 与 `BlindCandidate[]`。 |
| 程序环境 | 短合同 ≤6000 字单次；长合同最多 4 个 map + 1 个 reduce，reduce 可重试一次；调用前逐次扣预算。 |
| 权限 | 可建议段分、评语、既有 item 缺口和候选；不得改 `items[].status`，不得发明新 checklist item，模型分数不得绕过代码扣分和封顶。 |
| 验证门禁 | 必须先有规则结果与 scorecard 配置；JSON shape；禁语；服务端重算；非通过项点名完整性；候选只能落到目标 gap；quote 全文校验；去重和条数限制。 |
| 失败语义 | `no_rule_results / no_llm_key / no_scorecard_config / budget_exceeded / llm_error / parse_failed / incomplete_model_output`；全部保留规则结果。 |
| 当前日志 / coverage | 长合同记录 chunks total/reviewed、字符覆盖、未读范围与 limited；短合同无逐调用审计元数据。 |
| 当前测试 | scorecard 数值、封顶、禁语、坏结构、长合同分段、预算、候选 target / quote、API 透出均有 mock 回归。 |

### N2 验收标准

- 开关补盲前后 `items` 深度相等；
- 模型给满分也必须受规则硬伤扣分与封顶；
- 模型漏段、非数值、Infinity 或坏嵌套不得导致规则结果丢失；
- `coverage.limited=true` 时，报告和 UI 不得暗示已读全文；
- 任何候选必须对应允许的现有 item，并带可核原文；
- 若要允许“规则外全新争点”，必须新增独立 Candidate Issue 对象和人工受理流程，不能借用 `blind_candidates` 越界。

## 13. N3 — Quality：完整性、一致性与影响观察

| 项目 | 规范 |
|---|---|
| 触发事件 | model review 结束后，`QUALITY_ENABLED=true` 且正文可用。 |
| 角色 | 检查清单之外的分析顾问；输出待人工确认观察。 |
| 当前输入 | 文本 / 分段、规则 items、policies、品类、立场、条款目录；长合同一致性轮只看已清洗素材与 facts，不看全文。 |
| 标准输出 | `QualityInfo { observations[], facts[], pending_questions[], coverage, dropped_count }`。每条 observation 为 dimension/title/quote/comment/evidence/needs_confirm。 |
| 程序环境 | 短合同单次，坏结构或禁语重试一次；长合同最多 4 个 map，成功块 ≥2 可做一次一致性轮并允许一次重试；计入统一预算。 |
| 权限 | 可提出完整性、跨条款一致性和实际影响观察；不得计入评分、不得改规则档位、不得宣布免人工确认。 |
| 验证门禁 | dimension 白名单；quote 全文校验；clause 服务端派生；双禁语；字段限长；去重；最多 12 条且每维最多 6 条；`needs_confirm=true` 由代码强制。 |
| 失败语义 | `disabled / no_llm_key / budget_exceeded / llm_error / parse_failed / error`；整层可隐藏，但必须在状态中保留不可用原因。确定性 facts 仍可兜底。 |
| 当前日志 / coverage | 有 dropped_count、分段 coverage 与错误日志；无每条删除原因、模型/Prompt/耗时/token 审计。 |
| 当前测试 | 四道注入门禁、长合同 coverage、单块失败、预算、上限、API 与 `items/scorecard` 深度相等。 |

### N3 验收标准

- 每条展示观察都必须有可核 EvidenceRef；
- 观察为空是合法结果，必须区别于“节点未执行/执行失败”；
- 一致性轮只能使用已验证材料，不得借 reduce 轮重新生成不存在的 quote；
- 对不同立场的影响解释可以变化，原文事实与正式状态不能变化；
- 真实模型验收必须分别统计三维召回、无依据观察率、相关性拒收率和人工采纳率。

## 14. N4 — Objection：对规则结果提出受限异议

| 项目 | 规范 |
|---|---|
| 触发事件 | Quality / Verify 完成后，`OBJECTIONS_ENABLED=true`；只对受理矩阵允许的规则结果生成候选。 |
| 角色 | AI 律师；可举证和质疑，不可改判。 |
| 当前输入 | 最多 12 个 eligible items、真实档位、rule class、规则备注/摘句、立场、条款目录；omission 另获最多约 16000 字正文。 |
| 标准输出 | `ObjectionInfo`；每条包含 item/rule/class/direction/quote/counter_evidence/legal_reasoning/stance_check/proposal/accepted/reject_reason/clause/adopted。 |
| 程序环境 | 单次调用，坏结构或禁语重试一次；最多受理 6 条；计入统一预算且位于链尾，预算紧时优先牺牲。 |
| 权限 | heuristic 只可提 `false_positive`；existence 只可提 `omission`；hardline 与 N/A 不送审。可生成规则改进提案，不得修改当次 status。 |
| 验证门禁 | 送审集合比对；方向×类别矩阵；quote 存在并定位；证据属于允许条款范围；counter evidence；清洗后 reasoning ≥30 字；stance 自检；rule_id 由服务端决定。 |
| 失败语义 | `disabled / no_llm_key / budget_exceeded / llm_error / parse_failed / error`；不影响规则、评分与质量层结果。合法无候选为 `available=true + objections=[]`。 |
| 当前日志 / coverage | 候选维 `eligible / sent / reviewed / candidate_limited` + 正文维 `body_chars_total/sent、clauses_total/sent、body_limited`，rejected_count；正文范围已 span 级绑定（引用须落在实际发送区间）。仍无 provider/model/prompt/latency/token 逐调用账本。 |
| 当前测试 | class × direction、hardline 拒绝、候选外注入、真实但错 scope、正文截断、五要件、adopt 不改档位、corpus 对抗。 |

### N4 验收标准

- `(item_id, direction)` 是一个争点；不得靠更换 quote 重复占用异议名额；
- quote 和 counter evidence 都必须同时满足真实性与相关性；
- `accepted=true` 只表示通过程序性受理门槛，不表示异议实体成立；
- `adopted=true` 只表示人工愿意发起规则改进，不表示当次改判；
- 完整闭环完成的定义是：提案 → 专业复议 → Rule Pack 版本变更 → 金标/回放 → CI → Merge → 新版本重新审查；当前最后六步尚未自动化。

## 15. N5 — Ask：用户按需追问与建议改写

| 项目 | 规范 |
|---|---|
| 触发事件 | 审查记录存在，用户对 `status=需关注` 的 item 主动提交问题。 |
| 角色 | 解释者与谈判草拟助手。 |
| 当前输入 | 用户问题（≤500 字）、目标 item、policies、用户立场、目标完整条款及相邻条款；无法定位时回退 6000 字头尾背景。 |
| 标准输出 | 七字段回答：风险等级、这条在查啥、原文在哪、问题是啥、建议怎么改、改写稿、还想问；API 另带 `quote_verified` 与 EvidenceRef。 |
| 程序环境 | 同步单次调用；按 IP 默认 20 次/分钟；不计入 ReviewBudget；无会话记忆。 |
| 权限 | 可解释、建议、草拟一份文本；不得回答非“需关注”项，不得把改写稿标成已合规，不得修改合同、状态或核验记录。 |
| 验证门禁 | 问题非空与长度；目标状态；禁语清洗；字段类型收敛；quote 在条款上下文/正文核验；未核验则隐藏改写稿。 |
| 失败语义 | 无 Key、超限、供应商错误均返回明确错误；解析失败可退为受控 raw explanation，但不能伪造已核 quote。 |
| 当前日志 / coverage | 只有错误日志和 IP 限频；回答、provider/model、上下文范围、耗时、token、成本、用户反馈均未持久化。 |
| 当前测试 | 非需关注拒绝、问题长度、禁语、坏类型、quote 伪造、条款上下文、错误信息泄露、限频。 |

### N5 验收标准

- “改写稿”必须始终被标为建议草案；
- 未核验原文不得展示基于该原文的改写；
- 多轮追问上线前必须有会话对象、上下文摘要来源、轮次预算、删除/TTL 与冲突处理；
- 每审查 Ask 上限上线前必须解决与 review TTL 同步的计数清理；
- 任何“修改已通过”的表述，必须来自新版本文档重新进入正式审查，不得来自 Ask 自评。

## 16. N6 — Bounded Verify：明确的非 LLM 边界

| 项目 | 规范 |
|---|---|
| 触发事件 | Quality 产出后自动一轮，或用户手动 trigger / confirm / reverify。 |
| 当前角色 | 确定性调查与程序分流；不是模型 Agent。 |
| 当前输入 | items、quality observations、blind candidates、facts、clause index、document version、prior verify state。 |
| 标准输出 | `VerifyInfo`：round/fetch/question 预算、ConfirmQuestion、triage_log、人工选择、再核结果。 |
| 程序环境 | 进程内 review lock + SQLite 状态；默认 3 轮、8 问、24 次条款取证、每问 2 次再核。 |
| 权限 | 可取证、核对、分成 must_human / machine_ok / machine_silent；不得修改 rule item status。 |
| 验证门禁 | 文档版本、quote/span/clause、预算、人工动作前置、并发锁、items 快照不变断言。 |
| 失败语义 | error 或 budget_exceeded 明示；保留既有问题和规则结果。 |
| 当前测试 | 五类分流、证据错条款、金额一致性、预算、并发状态、confirm/reverify 不改档位。 |

未来允许 LLM 参与“建议下一步核对”时，模型只能生成 `InvestigationProposal`，由服务端校验后决定是否执行。模型不得自己递归调用条款读取、扩大轮次或把 `machine_ok` 当成正式通过。

---

# 第五部分：运行环境、配置与失败语义

## 17. 当前模型与传输环境

当前供应商优先级为：

1. DeepSeek；
2. 智谱 / GLM；
3. xAI / Grok。

三者使用 OpenAI-compatible `/chat/completions` 单轮 HTTP 请求。当前是共享服务端环境变量 Key；`AskAuthProvider` 只有未来 per-user credential 接口桩，尚未实现用户级凭据、租户隔离或权限映射。

模型用途只有两级：`PRECHECK` 与 `REVIEW`。Quality、Objection、Ask 当前都走 review 级模型。新节点不得私自新建第三套 provider 逻辑，应复用统一 transport，并先扩充用途枚举、配置验证和调用账本。

## 18. 配置权威表

| 配置 | 当前默认 / 语义 | 约束 |
|---|---|---|
| `PRECHECK_ENABLED` | `true` | 关闭只跳过分诊，不影响规则审查。 |
| `QUALITY_ENABLED` | `true` | 关闭后不得改变 items / scorecard。 |
| `OBJECTIONS_ENABLED` | `true` | 关闭后不得改变上游任何结果。 |
| `LLM_BUDGET_PER_REVIEW` | 代码默认 18；`0` 为不限 | 覆盖 precheck + model review + quality + objection；不覆盖 Ask 与 Verify。 |
| `LLM_REVIEW_MAX_SEGMENTS` | 4 | 影响评分/补盲长合同 map 数。 |
| `QUALITY_MAX_SEGMENTS` | 回落上项 | 只控制 Quality 长合同 map 数。 |
| `LLM_TIMEOUT_SECONDS` | 180 秒 | review 通用；当前非法字符串可能在调用期抛错，不是全部启动 fail-closed。 |
| `PRECHECK_TIMEOUT_SECONDS` | 30 秒 | Precheck 独立短超时。 |
| `OBJECTION_TIMEOUT_SECONDS` | 回落 180 秒 | 异议层独立覆盖。 |
| `RATE_LIMIT_UPLOAD_PER_MINUTE` | 10 | 启动校验；单进程、按 IP。 |
| `RATE_LIMIT_ASK_PER_MINUTE` | 20 | 启动校验；单进程、按 IP。 |
| `VERIFY_MAX_ROUNDS / QUESTIONS / CLAUSE_FETCHES / RECHECK_PER_Q` | 3 / 8 / 24 / 2 | 确定性核验预算，不是 LLM 预算。 |
| `STORE_TTL_HOURS` | 24 | 审查记录含全文与 AI 产物；`0` 为永久，必须谨慎。 |

配置要求：

- 配置模板、管理员文档与运行时代码必须在同一 PR 对账；
- 影响权限或失败语义的开关必须写入状态与测试，不得只靠 Prompt；
- 新增数值配置应在启动时 fail-closed 校验，不能等请求进入后才因 `float()` / `int()` 失败；
- 生产环境不得在日志、API 或报错中泄露 Key、完整供应商响应或敏感合同正文。

## 19. Fail-closed 与 Fail-soft

### 必须 fail-closed

- 文件解析失败，无法得到可信正文；
- 未知正式状态、非法 Rule Pack、正则或 class；
- 准备写入正式证据但真实性/版本不成立；
- LLM 请求试图越权修改 `items[].status` 或规则元数据；
- 外部执行的授权、目标、金额或文档版本不明确；
- 认证半配置或关键访问控制失效。

### 可以 fail-soft

- 无 LLM Key；
- 可选参考层超时或供应商失败；
- 单块 map 失败但 coverage 能诚实显示；
- 可选异议、质量或评分不可用；
- 预算耗尽。

Fail-soft 的统一含义是“该能力未完成或不可用”，不是“已经检查且没有问题”。`available=false + reason`、`coverage.limited`、`completion` 和 UI 文案必须保持一致。

## 20. 标准 reason code

现有代码已使用但尚未集中成枚举的主要 reason 包括：

`disabled`、`no_llm_key`、`busy`、`no_rule_results`、`no_scorecard_config`、`budget_exceeded`、`llm_error`、`parse_failed`、`incomplete_model_output`、`error`、`not_attempted`、`empty`、`triaged_clear`。

下一阶段必须把 reason code 收敛为共享类型，并明确：

- 哪些表示未执行；
- 哪些表示执行失败；
- 哪些表示合法空结果；
- 是否可重试；
- 是否消耗预算；
- UI 是否展示；
- 是否影响 `completion`。

---

# 第六部分：日志、Coverage 与审计标准

## 21. 当前已经具备

- 服务端异常日志，不向客户端回传供应商内部错误；
- ReviewBudget 的内存调用次数控制；
- Scorecard / Quality 的长合同 coverage；
- Objection 的候选维 + 正文维 coverage（candidate_limited / body_chars_* / clauses_* / body_limited）；
- Quality dropped_count 与 Objection rejected_count；
- Review `stage` 与 `completion`；
- EvidenceRef 的文档版本与坐标；
- Verify 的 rounds / clause fetches / triage log。

## 22. 当前缺失，列为 P0

每次 LLM 调用必须新增可持久化、可脱敏的 `LLMCallRecord`，至少包含：

```json
{
  "call_id": "stable-id",
  "review_id": "review-id",
  "node": "precheck | model_map | model_reduce | quality_map | quality_reduce | objection | ask",
  "provider": "deepseek | zhipu | xai",
  "model": "resolved-model-name",
  "prompt_version": "content-hash-or-semver",
  "input_scope": {
    "document_version": "...",
    "clause_ids": ["c01", "c02"],
    "chars_sent": 3200,
    "truncated": false
  },
  "attempt": 1,
  "started_at": "timestamp",
  "latency_ms": 1240,
  "outcome": "success | timeout | provider_error | parse_failed | gate_rejected | budget_exceeded",
  "usage": {"input_tokens": null, "output_tokens": null, "cost": null},
  "gate_counts": {"accepted": 2, "rejected": 1},
  "retention_class": "review-ttl"
}
```

要求：

- 不记录 API Key；默认不记录完整 Prompt 和完整合同正文；
- 需要复现时记录 prompt version、输入范围和内容 hash；
- provider 返回 token usage 时应记录，未返回填 null，不得伪估成精确值；
- Ask 也必须进入调用账本，不能成为观测盲区；
- 调用记录 TTL 不得长于合同数据，除非经过脱敏与独立合规批准；
- 指标应能回答：调用了几次、读了哪里、失败在哪里、哪些输出被门禁拒绝、花了多久和多少钱。

## 23. Coverage 的统一口径

每个读取合同文本的 LLM 节点都应报告：

- `original_chars`；
- `chars_sent` / `chars_covered`；
- `clause_ids_sent`；
- `chunks_total / chunks_reviewed`；
- `unread_ranges` 或等价未读条款；
- `truncation_reason`；
- `limited`。

Precheck 和 Ask 当前缺少这一统一对象；Objection 已在自身 coverage 内记录正文范围（span 绑定 + body_chars/clauses/body_limited），但尚未收敛到公共 Coverage schema。以上均为下一阶段必须补齐的 P0。

---

# 第七部分：测试与发布门禁

## 24. 当前测试能证明什么

当前 required CI 可以证明：

- 规则与状态不因 mock LLM 输出而被越权修改；
- 多类坏 JSON、禁语、错误 scope、伪造 quote、预算与截断路径被程序门禁处理；
- API schema、前端 E2E、Ruff 与 Pyright 基线通过；
- 无 Key 环境不会误发真实模型请求。

当前 required CI 不能证明：

- 某真实模型对合同分类、漏报、误报或法律逻辑达到业务标准；
- 真实供应商在当前模型版本、超时和长合同下稳定；
- 不同 provider 的输出质量等价；
- 用户能正确理解 AI 观察、异议和机器分流；
- 多文档 RAG 或修订验证已经存在。

## 25. 每个新 LLM 节点的最低测试包

任何新增或扩权节点必须同时提供：

1. **纯函数单测**：schema、白名单、长度、数量、去重与状态转换；
2. **权限不变式**：节点开 / 关、成功 / 失败前后正式状态深度相等；
3. **恶意模型输出**：越权字段、错误类型、重复、超长、禁语、伪造 id；
4. **证据测试**：假 quote、真实但错 scope、多处命中、错版本、模型未见文本；
5. **预算测试**：首次调用、重试前、map 中途、reduce 预留和耗尽；
6. **失败测试**：timeout、HTTP error、空响应、坏 JSON、部分成功、服务重启；
7. **coverage 测试**：全文、截断、单块失败、全部失败、回退路径；
8. **API / Store 测试**：旧记录兼容、持久化、TTL、并发写与 completion；
9. **前端 E2E**：不可用、有限覆盖、待确认、空结果与错误提示；
10. **真实模型专项**：按 provider/model 记录语料版本、成功率、延迟、成本、误报/漏报和人工评分。

## 26. 发布结论的最低措辞

- 只有 mock 回归通过：可写“程序门禁已实现并通过确定性测试”。
- 真实模型专项通过但无生产数据：可写“在版本化验收集上达到阈值”。
- 有生产与人工复核数据：才可写“在指定场景中已验证”。
- 不得只凭 CI 绿色写“模型准确”“已读懂全文”“可以替代法务”。

---

# 第八部分：分阶段开发优先级与验收标准

## 27. P0 / 阶段 A：把现有 LLM 变成可审计的受控能力

**目标：先让开发组能准确回答“哪次调用、看了什么、由谁调用、产出为何被接受或拒绝”。**

必须完成：

- 统一 `LLMCallRecord`、Coverage、reason code 与 prompt version；
- provider / model / timeout / segment / budget 配置启动校验；
- Precheck、Scorecard、Quality、Objection、Ask 全部接入调用账本；
- Ask 纳入 review 级调用上限或独立可清理预算；
- 明确 ReviewBudget 的优先级策略，不再只依赖“谁先调用谁先拿”；
- 补齐 UI 对 limited / unavailable / skipped 的一致表达；
- 建立小规模真实模型验收工作流，但不把付费 live test 放入每次普通 CI。

验收：

- 给定 review id，可以还原所有 LLM 节点、实际模型、输入条款范围、尝试次数、失败原因、门禁结果与预算余额；
- 任一节点关闭、失败或预算耗尽，规则 items 字节级不变；
- 所有有限覆盖都能在 API 和 UI 看见；
- 配置模板、管理员文档、代码默认值自动对账或有测试钉死；
- 真实模型验收报告可按模型版本复现。

## 28. P1 / 阶段 B：完成单合同的 Evidence → Claim → Human Decision 模型

**目标：把“很多带 quote 的字符串”升级为可复用、可追踪的证据与争点。**

必须完成：

- 引入稳定 `evidence_id`（已完成：证据法批 1）、`claim_id / issue_id`（待做）；
- 明确支持/反驳关系、证据 scope 与模型可见范围；
- Objection、Quality、Ask、Verify 复用同一 Evidence，而不是各自复制 quote；
- 人工确认形成独立 `HumanDecision`，不得写回模型字段；
- 异议去重键至少稳定到 `(item_id, direction)`；
- adopt 后生成版本化规则改进工单，仍由人评审、回放与合并；
- 对 evidence / claim / human decision 做迁移与旧记录兼容。

验收：

- 一个风险争点从规则命中到解释、异议、人工确认和报告始终引用同一证据对象；
- 任何结论均可追到文档版本、条款、span、可见范围和提出者；
- 人工“确认事实”“接受异议”“接受剩余风险”是三种不同动作；
- 重复 quote 不再制造多个独立异议或待办；
- 旧审查记录仍可读取且不会被错误升级为新证据等级。

## 29. P1 / 阶段 C：完成“修改—再审”闭环

**目标：让 Agent-T 不只给建议，而能验证新版本是否真的解决问题。**

必须完成：

- 合同版本对象与父子关系；
- A/B/C 修改方案及适用条件；
- 采纳方案后生成新版本或明确的 patch；
- clause-level diff、引用方与连带影响分析；
- 新版本必须重新进入 parse → checklist → AI reference → verify；
- 展示“已解决 / 仍存在 / 新增风险 / 无法判断”，并保留原版本结果；
- 禁止 LLM 对自己生成的改写稿自我盖章。

验收：

- 修改一个付款条款后，相关验收、发票、违约、期限引用会被重新检查；
- 正式状态只来自新版本的 Rule Engine；
- 用户可以比较两个版本的证据、状态和未解决事项；
- 回退到旧版本不会丢失任何确认和审计记录；
- 真实合同版本测试覆盖局部修订、编号变化、条款移动和删除。

## 30. P2 / 阶段 D：多文档 Evidence Workspace 与受限 RAG

**目标：从“合同写了什么”扩展到“项目实际发生了什么”，但不把向量相似度当证据。**

前置条件：阶段 A、B、C 全部完成。必须建设：

- 文档、版本、来源、权限、有效时间和证据等级；
- query plan 与可审计检索记录；
- 精确引用、页码 / 坐标、版本与权限过滤；
- 合同条款、附件、验收、付款、变更、往来函件之间的关系；
- 冲突证据、过期证据、缺失材料与不确定性；
- 人工可确认的 retrieval / evidence gate；
- RAG 评测集：召回、相关性、版本正确性、权限泄漏为零。

验收：

- 回答“能否支付 95%”时能分别引用合同条件、最新变更、验收记录和整改状态；
- 检索不到材料时明确说“缺什么”，不得补写事实；
- 旧版本或无权限文档不得进入模型上下文；
- 每个回答可列出实际检索到、被过滤和最终采用的证据；
- 任何自动结论仍受 Rule / Human 权限边界约束。

## 31. P3 / 阶段 E：受控工具与外部执行

**目标：在有独立授权、回滚和审计之后，才把确认结果送往外部系统。**

本阶段默认不进入当前 MVP。任何立项必须先提交 Constitution impact，至少说明：

- 谁授权；
- 哪个确定版本和结论可执行；
- 金额、对象、收件人和环境如何二次确认；
- dry-run、幂等、撤销和补偿机制；
- 审计记录与人工终止开关；
- 模型能否选择工具、参数和执行时机。

没有这些条件，LLM 只能草拟动作建议，不能执行。

---

# 第九部分：Roadmap 对账与开发纪律

## 32. 与现有 roadmap 的对账

截至本文代码基线：

- roadmap 阶段 0.5、1.1、1.2、1.3、2.1、2.2、2.3、2.4 已有对应实现；
- 阶段 3 的 A6 有界主动核验已实现，但它是确定性核验，不是 LLM 自治调查；
- 阶段 3.1 / 3.2 的 LLM 异议产出、五要件受理和 adopt 标记已经进入 `main`；
- 阶段 3.3 的 mock / corpus 对抗门禁已经存在，但真实模型与专业验收仍属部分完成；
- 阶段 4 的 A/B/C、连带影响与新版本重审未实现；
- 多文档 RAG 仍是长期方向，未实现。

以后 roadmap 的“✅”至少应注明属于：程序实现、mock 验收、真实模型验收、生产验证中的哪一级。

## 33. PR 必填清单

任何涉及 LLM 的 PR，除 Constitution 清单外，还必须回答：

1. 节点编号与角色是什么？
2. 触发事件是什么？同步还是后台？
3. 模型实际能看到哪些文档、条款和用户输入？
4. 标准输入 / 输出对象与 schema version 是什么？
5. 模型新增了什么权力，明确没有什么权力？
6. 哪些字段由服务端重新计算或派生？
7. 真实性、相关性、scope、版本和 coverage 如何验证？
8. 超时、无 Key、预算耗尽、坏 JSON、部分成功分别是什么语义？
9. 调用次数、延迟、token、成本和拒收原因如何记录？
10. 哪些 mock、对抗、live、E2E 与旧记录测试证明它可合并？
11. 是否改变正式结果、人工确认或外部执行边界？若是，`Constitution impact` 在哪里？
12. README、管理员配置、roadmap 与中英文治理入口是否同步？

## 34. Definition of Done

一个 LLM 节点只有同时满足以下条件，才可以宣布“完成”：

- 权限边界写进代码结构，而不只写进 Prompt；
- 输入范围和标准对象已版本化；
- 服务端验证、失败语义与 coverage 已实现；
- 调用预算、并发、超时、限频与数据保留已定义；
- 结构化调用日志已接入；
- mock / 对抗 / API / Store / E2E 测试通过；
- 真实模型验收达到事先定义的阈值；
- 用户界面不会把参考意见显示成正式结论；
- 文档与实际代码一致；
- Constitution impact 已判断并记录。

若只完成其中一部分，应明确写“程序已实现”“试验性”“部分完成”或“尚未生产验证”，不得用一个绿色勾号抹平成熟度差异。

---

# 结语

Agent-T 不以“让 LLM 接管合同审查”为目标，而以“让 LLM 在有证据、有程序、有权限边界、有人工责任的系统中发挥认知能力”为目标。

因此，后续开发的判断标准不是模型说得多像专家，而是：

> **它在正确的事件被调用，只看被授权的上下文，只产生被允许的对象，经过可验证的门禁，在失败时诚实降级，并且永远不能靠自己的文字扩大权力。**
