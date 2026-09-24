# 证据法批 2b-③ 专项实现稿：counter_evidence 票据化 + Ask 引用入库 + 三路并发

> 状态：**v1.0 初稿待审**（2026-09-24，摸底基线 main=98a0a95，含 PR #76 签收修正）。
> 前置：2b-② 已独立验收通过并合并（PR #74/#76，审计签收 2026-09-23）。
> 节奏：**只审设计、不直接开工**。本稿为开工放行的唯一依据；Q1-Q4 为审计裁决项。
> 分隔符记法：`<US>`=U+001F（实现用 chr(31) 显式构造，禁字面隐形控制字符）。

## 0. 范围一句话与批次边界

**本批做**（总设计稿 §5-2b 既定切分，非追加需求）：
1. **counter_evidence 票据化**——六层里最后一个裸字符串引用补票，present/absent/missing 三态分离；
2. **Ask 引用入库**——追问票据落 row 内账本（总设计稿 §4.9 隐私 TTL 四规则）；
3. **三路并发测试**——Ask+Verify+Objection 并发写互不覆盖（2b-② 声明的「不虚交」欠账）。

**不做**：citation_edges / decision_history（2c）；Ask 回答全文落库与对话回放（永久红线）；pending 稳定对象键（延后，本批不阻塞）；Ask 层发 claim_id（见 §3.6，防走样声明）。

## 1. 现状摸底（2026-09-24 @ 98a0a95，全部代码实证）

### 1.1 counter_evidence：裸字符串链路

| 环节 | 位置 | 现状 |
|---|---|---|
| 提示词 | prompts/objection.py:29/54/84 | 要求模型给反证原文**连续摘录**，或声明「未发现反证原文」（`_DECLARATION`，objection.py:55） |
| 受理校验 | objection.py:408-415 | 要件②：counter 非空；非声明时须过 `quote_supported` 全文核验 + `allowed_spans` 范围校验 |
| 入库 | objection.py:605 | `Objection.counter_evidence`（裸字符串，截 MAX_QUOTE_CHARS）——**无票据无 ID** |
| 主票据 | objection.py:587 | 受理异议已有 `build_evidence(parse_source="objection")`（2b-② 挂 claim_id/rebuts） |

### 1.2 Ask：票据生成但零落库

- llm_ask.py:300-325 建票（`parse_source="ask"`）**只随响应返回**；routes.py:474-517 的 ask 端点无任何 store 写入——重启即丢，全链无账。
- 附带发现：llm_ask.py:302-322 两次 `build_evidence`，第一次结果纯丢弃（冗余，见 Q4）。
- llm 调用账本已有（`llm_call_log.record_node`，routes.py:489）——那是**调用账**，不是**证据账**，不互替。

### 1.3 锁纪律与 TTL 先例

- verify 三端点（routes_verify.py:102/145/186）：`lock_for(review_id)` 锁内 store 读改 + `_merge_evidence_index`——本批 Ask 写入必须复用同一把锁、同一管线。
- TTL：`STORE_TTL_HOURS`，`0 = 永不过期`（store.py:88，过期判定短路）。

## 2. counter_evidence 票据化

### 2.1 三态语义（absent/missing 分离是本批核心——两者常被混为一谈）

| 状态 | 判定（服务端结构位置决定，模型声明一律不作数——声明只影响 absent 的**识别**） | 票据 | ID |
|---|---|---|---|
| `absent` | counter == `_DECLARATION`（「未发现反证原文」合法声明） | 无票 | 无 |
| `present` | counter 非声明且 `locate_quote_span` → verified/ambiguous → `build_evidence(parse_source="objection")` | 有票 | 有 |
| `missing` | counter 非声明但定位失败/unverified | 无票 | **无**（fail-closed，与批 1「missing 无资格 ID」同哲学） |

- **absent ≠ missing**：absent 是「模型声明没有反证」（合法语义态）；missing 是「有反证原文但服务端无法定位到原文」（资格态缺失）。混用会让「没找到反证」和「找到了但钉不住」在账目上无法区分——决定链审计必须能区分。
- 判定只发生在**受理异议**建票阶段（objection.py:587 邻域）；未受理异议整条无主票，counter 不建票（账目噪声控制）——**Q1 裁决：未受理异议是否也要 counter 票？本稿建议不建**。
- 范围校验（allowed_spans）沿用 _validate 既有结果：受理即已过范围校验，建票阶段不重复校验。

### 2.2 schema 与兼容

- 服务层 `Objection` + API 层 `ObjectionInfo` 增 `counter_evidence_status: str = ""`（枚举 `present|absent|missing`，空串=旧记录未知）。
- **向后兼容红线**：旧记录无此字段 → 读路径**不回填不推断**，保持空串——旧行响应除新增键外逐字节一致。空串语义「本批之前的记录，状态未知」，与 legacy_scope 警告同哲学：不冒充已知。
- **Q2 裁决：旧记录（尤其 2b-② 后未受理 counter 的存量）空串是否进迁移警告账本？本稿建议：不进**（counter 状态是展示/审计辅助，不进 claim 身份链，不构成迁移事件）。
- `counter_evidence` 裸字符串**保留**（展示用事实源，总原则「内嵌不删」）。

### 2.3 引用边（annotate_review_claims 扩展）

- 受理异议且 counter 票合格（ID 非空形状合法）→ `evidence_refs` 追加 `{"evidence_id": <counter 票 ID>, "relation": "counter"}`。
- **不变式**：一主张至多一个 primary（既有）+ 至多一个 counter；counter 与 rebuts 独立记账——counter 缺失（absent/missing/空串）**不影响** `rebuts_status`（两本账，语义无关：rebuts=「我反对规则项」，counter=「支持我反对的证据」）。
- 业务主键**不变**：objection 业务键仍为 `item_id + <US> + direction + <US> + primary evidence_id`——counter 不进键（同一异议换反证不换 claim_id，反证自身身份由票据 ID 承载）。
- `claim_content_hash` 白名单**不变**（objection = legal_reasoning/proposal/stance_check，v1.9 既定）——counter_evidence 字符串本就不进指纹，本批不扩。

## 3. Ask 引用入库（总设计稿 §4.9 四规则逐条落地）

### 3.1 存什么（规则一：只存引用账目，不存回答）

```text
row["ask_evidence"] 追加元素（白名单字段，五键固定）：
{
  "asked_at": <ISO8601>,
  "item_id": <清单项 id>,
  "question": <问题文本，截断 ≤200 字>,
  "evidence": <完整票据 dict（ID/坐标/状态/版本）>,
  "quote_verified": <bool>
}
```

- **永不存** `answer` / `raw_text`——「回答本来就不落库」的隐私边界不因证据追踪而破坏（§4.9 原文）。截断 ≤200 字与 §4.9 一致。
- 元素顺序 = 追加时序（append-only）。

### 3.2 写在哪（规则二：寄生行内，同 TTL）

- 写入点：routes.py `ask()`，`llm_call_log.record_node` 块之后：`ok=True 且 evidence 非空` 时，`verify_service.lock_for(review_id)` 锁内 `store.update` 追加 + `_merge_evidence_index(review_id)`（Ask 票进证据索引，与 verify 同管线、同一把锁）。
- 无独立生命周期、无独立表——随行级 TTL 过期。

### 3.3 TTL=0 不登记（规则四：宁缺毋滥）

- `store` TTL 为 0（永不过期）时**不写** ask_evidence（防「追问证据无限期保存」）；判定走 store 公开属性（如需暴露 `ttl_seconds` 只读属性，属最小接口新增），不读私有变量。
- 此时 Ask 响应行为不变（票据照常随响应返回），只是不入账。

### 3.4 可见性（规则三：计数可见，不回放）

- `ReviewSummary` 增 `ask_evidence_count: int = 0`——get_review 派生（len(ask_evidence)），**明细永不下发**（隐私+体积）。
- 前端本批不动（计数展示留 2c 报告闭环一起做）；API 字段先到。

### 3.5 防刷上限

- 每 row `ask_evidence` 上限 **50** 条（`MAX_ASK_EVIDENCE_ENTRIES`，环境变量可调）；超限后**新追问照常回答、不再入账**（不计错误、响应不变——账本是观测设施，不能反过来掐断用户功能）。
- **Q3 裁决：50 合适吗？**

### 3.6 Ask 不发 claim_id（防走样声明）

- Ask 层只有证据票据，**没有主张对象**（三层模型 Evidence/Claim/Decision 无 ask claim_type）——本批不引入「ask 主张」，不为其发 claim_id、不进决定链。若未来出现 ask 主张，须作为新 claim_type 显式定义（与 §1.1 五类同规矩）。

## 4. 三路并发测试（2b-② 声明的「不虚交」欠账）

- **T-C1**：同一 row 并发三路——k 路 Ask 入库（≥2）、1 路 verify confirm、1 路 objection adopt → 全部生效、无互相覆盖、evidence_index 与各行内容一致（锁纪律回归：任何一路绕锁 = 测试红）。
- **T-C2**：Ask 入库与读路径并发（get_review 连发）→ 读路径幂等、store 无撕裂。
- 实测并发用 threading.Barrier 对齐起跑（沿用 conftest 槽位排空纪律，防 429 污染）。

## 5. 测试清单与变异验证（红线：回滚必须变红）

| 编号 | 钉子 | 变异 |
|---|---|---|
| T-A1 | absent：声明 → status=absent、无票无边 | 改 absent 判定为恒 missing → 红 |
| T-A2 | present：反证原文定位成功 → 有票有 ID、refs 含 counter 边 | 去掉 counter 边生成 → 红 |
| T-A3 | missing：反证原文定位失败 → status=missing、**无 ID**、无边 | 改为发空 ID/伪 ID → 红 |
| T-A4 | 旧记录兼容：无 status 字段 → 空串、响应除新键外逐字节一致 | 回填推断 → 红 |
| T-A5 | counter 缺失不影响 rebuts_status（两本账独立） | 联动 → 红 |
| T-B1 | ask 成功 → row 账本追加五键白名单元素；answer/raw_text **零字节**落库 | 把回答写进账本 → 红 |
| T-B2 | question >200 字截断；上限 50 条后不再入账且响应正常 | 去掉上限 → 红 |
| T-B3 | TTL=0 不登记 | 改为登记 → 红 |
| T-B4 | ask_evidence_count 派生正确；明细不下发 | 下发明细 → 红 |
| T-B5 | Ask 票进 evidence_index（_merge 后索引含 ask 来源票） | 绕过合并 → 红 |
| T-C1/C2 | 三路并发 + 并发读（§4） | 任一路绕锁 → 红 |
| T-D1 | AskResponse schema 不变（旧客户端兼容）；服务模型字段仅增不改 | 剥既有键 → 红 |

## 6. 明确不做

- Ask 回答全文 / 对话回放 / 会话持久化（既有挂账独立处理，永久红线不因本批松动）
- citation_edges / decision_history / cited_by 视图（2c）
- pending 稳定对象键（延后；pending 问题照旧不发正式编号）
- 前端计数展示 UI（字段先到，展示随 2c 报告闭环）

## 7. 审计裁决项汇总

| # | 问题 | 本稿建议 |
|---|---|---|
| Q1 | 未受理异议是否建 counter 票？ | 不建（账目噪声控制；受理异议才进决定链视野） |
| Q2 | 旧记录 counter_evidence_status 空串是否进迁移警告？ | 不进（非身份链事件；与 A4 兼容钉配套） |
| Q3 | ask_evidence 每 row 上限？ | 50（env 可调） |
| Q4 | llm_ask.py:302-322 票据双建冗余（第一次 build 纯丢弃）是否本批顺手删？ | 删（同文件同函数、行为等价、单测覆盖在 T-B1；不属「扩展架构」） |
