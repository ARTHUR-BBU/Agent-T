# 证据法批 2b-③ 专项实现稿：counter_evidence 票据化 + Ask 引用入库 + 三路并发

> 状态：**v1.1 修订稿**（2026-09-24，按审计退回意见全文重写；v1.0 判词「设计退回修订，不批准进入代码施工」——四根承重梁全部补齐，对账见 §9；**外审放行 2026-09-25「有条件放行进入代码施工」，四条施工钉子见 §10**）。摸底基线 main=98a0a95（含 PR #76 签收修正）。
> 前置：2b-② 已独立验收通过并合并（PR #74/#76，审计签收 2026-09-23）。
> 节奏：**只审设计、不直接开工**。本稿为开工放行的唯一依据。Q1-Q4 审计已裁决（§8）。
> 分隔符记法：`<US>`=U+001F（实现用 chr(31) 显式构造，禁字面隐形控制字符）。

## 0. 范围一句话与批次边界

**本批做**（总设计稿 §5-2b 既定切分，非追加需求）：
1. **counter_evidence 票据化**——六层里最后一个裸字符串引用补票，present/absent/missing 三态分离，票据有真实存放字段；
2. **Ask 引用入库**——追问票据落 row 内账本（总设计稿 §4.9 隐私 TTL 四规则），并接入登记簿与索引；
3. **三路并发测试**——Ask+Verify+Objection 并发写互不覆盖，测试必须能制造真实写入冲突。

**不做**：citation_edges / decision_history（2c）；Ask 回答全文落库与对话回放（永久红线）；pending 稳定对象键（延后，本批不阻塞）；Ask 层发 claim_id（§3.6 防走样声明）。

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
- 附带发现（Q4 已裁决删除）：llm_ask.py:302-322 两次 `build_evidence`，第一次结果纯丢弃。
- llm 调用账本已有（`llm_call_log.record_node`，routes.py:489）——那是**调用账**，不是**证据账**，不互替。

### 1.3 登记簿与索引的现有扫描结构（v1.0 缺口——本批必须显式接线）

- `build_evidence_registry`（evidence.py:520）与 `rebuild_evidence_index`（evidence.py:693）都只扫描**固定容器清单**：items / blind_candidates / quality.observations / quality.facts / facts / objections(主票) / verify.questions。**不扫 ask_evidence（尚不存在）、不扫异议反证**——Ask 入账若不接线，「票进了账本但登记簿索引查无此票」。
- 登记簿有一条自检与 Ask 隐私设计**相撞**（v1.0 未预见）：「缺 quote 却挂合格标签 → broken」（evidence.py:560 邻域，批 2a PR review P2-b）——Ask 账本按 §3.2 **不存 quote**，若不豁免，所有 Ask 票会被误判坏票。豁免规则见 §4.1。
- 锁纪律先例：verify 三端点（routes_verify.py:102/145/186）`lock_for(review_id)` 锁内 store 读改 + `_merge_evidence_index`。
- TTL：`STORE_TTL_HOURS`，`0 = 永不过期`（store.py:88）；环境变量非法值启动即报错（store.py:28-41 fail-fast 先例，§3.5 沿用同哲学）。

## 2. counter_evidence 票据化

### 2.1 四态表（absent/missing 分离 + 未受理独立态；审计定稿口径）

| 情况 | 状态 | 发票？ |
|---|---|---|
| 已受理，模型明确声明未发现反证（== `_DECLARATION`） | `absent` | 否 |
| 已受理，反证原文定位成功（verified/ambiguous） | `present` | **是** |
| 已受理，模型给了反证但服务端定位失败（missing/unverified） | `missing` | 否（fail-closed，与批 1「missing 无资格 ID」同哲学） |
| **未受理异议** | **空串 `""`** | 否 |

- **未受理不得标 `missing`**：missing 表示「系统尝试验证过但失败」；未受理异议可能根本没进入反证验证流程（要件①已倒下即拒绝），两者混同会让账目说谎。空串语义 = 「本条未走反证资格判定」。
- **missing 不许越界**：状态只表示「反证未获票据资格」，**不得改变** `accepted` / `rebuts_status` / claim_id / `claim_content_hash`——四本账各记各事。
- absent/missing 判定只发生在**受理异议**建票阶段（objection.py:587 邻域）；范围校验沿用 _validate 既有结果（受理即已过 allowed_spans 校验），建票阶段不重复校验。

### 2.2 反证票据的存放字段（v1.0 承重梁一：票必须有档案柜位置）

新增显式字段，与主票 `evidence` 平行：

```text
服务层 Objection：        counter_evidence_ref: Optional[EvidenceRef] = None
API 层 ObjectionInfo：    counter_evidence_ref: Optional[EvidenceRefInfo] = None
```

**六个接线点全部显式清单**（缺一即「有箭头无票据」）：

| # | 接线点 | 动作 |
|---|---|---|
| 1 | 服务层 `Objection`（objection.py:66 邻域） | 增字段；受理且 present 时 = `build_evidence(parse_source="objection")` 产物 |
| 2 | API 层 `ObjectionInfo`（schemas.py:304 邻域） | 增同名字段（pydantic 不剥——批 1 静默剥字教训，T 钉断言往返存活） |
| 3 | 读路径归一化 `normalize_review_evidence` | 六容器遍历扩展：受理异议的 `counter_evidence_ref` 走与主票同一归一化（缺坐标补定位/坐标不实重定位/不合格清 ID，fail-closed） |
| 4 | 证据登记簿 `build_evidence_registry` | 扫描 `counter_evidence_ref`（容器标签 `objection_counter`，§4.1） |
| 5 | 索引 `rebuild_evidence_index` | 扫描 `counter_evidence_ref`（§4.2） |
| 6 | claim 标注 `annotate_review_claims` | counter 引用边从此字段派生（§2.3） |

- `counter_evidence` 裸字符串**保留**（展示用事实源，总原则「内嵌不删」）；票据是叠加的关系层。
- **向后兼容红线**：旧记录无新字段 → 读路径不回填不推断，`counter_evidence_status` 保持空串、`counter_evidence_ref` 保持 None——旧行响应除新增键外逐字节一致。

### 2.3 引用边（annotate_review_claims 扩展）

- 受理异议且 `counter_evidence_ref` 过资格校验 → `evidence_refs` 追加 `{"evidence_id": <counter 票 ID>, "relation": "counter"}`。
- **资格校验与 primary 同一套**（复用 `_valid_primary` 同源逻辑）：票 verified/ambiguous + ID 形状合法 + `document_version` 与行严格相等（签收修正 P1 同款跨合同防线）——不过则无边，**status 不改写**（写路径事实保留；票坏在登记簿 broken_refs 可见，账目以边为准）。
- **不变式**：一主张至多一个 primary + 至多一个 counter；counter 与 rebuts 独立记账——counter 为 absent/missing/空串/资格不足**不影响** `rebuts_status`（rebuts=「我反对规则项」，counter=「支持我反对的证据」，两本账）。
- 业务主键**不变**：`item_id + <US> + direction + <US> + primary evidence_id`——反证不进键（同一异议换反证不换 claim_id，反证身份由票据自身 ID 承载）。
- `claim_content_hash` 白名单**不变**（objection = legal_reasoning/proposal/stance_check，v1.9 既定）。

## 3. Ask 引用入库（总设计稿 §4.9 四规则逐条落地）

### 3.1 存什么（规则一：只存引用账目，不存回答）

```text
row["ask_evidence"] 追加元素（五键白名单，固定）：
{
  "asked_at": <ISO8601>,
  "item_id": <清单项 id>,
  "question": <问题文本，截断 ≤200 字>,
  "evidence": <票据 dict，字段白名单见 §3.2>,
  "quote_verified": <bool>
}
```

- **永不存** `answer` / `raw_text`——「回答本来就不落库」的隐私边界不因证据追踪而破坏（§4.9 原文）。T-B1 钉断言账本元素与 `answer`/`raw_text` 零交集。
- 元素顺序 = 追加时序（append-only）。

### 3.2 票据字段白名单冻结（v1.0 承重梁三：字段逐一列名）

Ask 账本内的 `evidence` **只存七键、不存 quote**（原文已在行 text 里，重复保存原文摘句既扩隐私面又增体积）：

```text
evidence_id / document_version / clause_id / start / end / verification / parse_source
```

### 3.3 写入门禁（不合格票据不入账）

同时满足以下四条才允许写入账本；任一不满足 → **Ask 照常回答、正常返回票据，只是不入账**（不报错、响应不变）：

1. `evidence_id` 非空；
2. `verification ∈ {verified, ambiguous}`；
3. `evidence.document_version` 与行 `document_version` **严格相等**（跨合同防线同款）；
4. 写入前以 `normalize_evidence_ref(ev, text)` 复核通过（坐标可在当前合同重核的确定性判据；normalize 是纯函数，复核副本不入参突变）。

unverified / 空票 / 跨版本票 → 不入账。**账本是观测设施，不能反过来掐断用户功能。**

### 3.4 写入路径（v1.0 承重梁四前半：全程同一把锁）

routes.py `ask()`：`llm_call_log.record_node` 块之后，`ok=True` 且通过 §3.3 门禁时——

```text
verify_service.lock_for(review_id):          # 与 verify 三端点同一把锁
    读 row → 追加 ask_evidence 元素 → store.update 写回
    → _merge_evidence_index(review_id)       # Ask 票进索引，同一锁内完成
```

读取→追加→写回→重建索引**四步缺一不可、全程锁内**（§5 冲突测试即钉此）。无独立生命周期、无独立表——随行级 TTL 过期（§4.9 规则二）。

### 3.5 TTL=0 不登记 + 上限严格校验（规则四：宁缺毋滥）

- store TTL 为 0（永不过期）时**不写** ask_evidence（防「追问证据无限期保存」）；判定走 store 公开只读属性（如 `ttl_seconds`，最小接口新增），不读私有变量。
- 每 row 上限 `MAX_ASK_EVIDENCE_ENTRIES`，默认 **50**（Q3 已裁决）；环境变量**严格校验**：负数/非数字/空串 → 启动即报错并写明合法范围（照 store.py:28-41 fail-fast 先例，不静默容错）。超限后新追问照常回答、不再入账、不计错误。

### 3.6 可见性 + 防走样声明

- `ReviewSummary` 增 `ask_evidence_count: int = 0`——get_review 派生（len），**明细与问题内容永不下发**（§4.9 规则三：计数可见，不回放）。前端本批不动，字段先到。
- **Ask 不发 claim_id**：Ask 层只有证据票据，无主张对象（三层模型无 ask claim_type）。不进决定链。若未来出现 ask 主张，须作为新 claim_type 显式定义。

## 4. 登记簿与索引接线（v1.0 承重梁二：票进账本必须查得到）

### 4.1 登记簿 `build_evidence_registry`

- 新扫两容器：`objections.objections[].counter_evidence_ref`（标签 `objection_counter`）与 `row["ask_evidence"][].evidence`（标签 `ask`）。
- Ask 票**全额参与计数**：`occurrence_total` / `qualified_occurrence_total` / `unique_total`；同 span 跨层命中（如 Ask 与规则层同 span）照常进 `multi_source_unique`——「跨层同证据可见」正是登记簿的本职。
- **quote 自检豁免（v1.0 未预见的交叉点）**：登记簿既有自检「缺 quote 却挂合格标签 → broken」对 `ask` 来源票**不适用**——quote 无残留是 §3.2 的设计决定，不是票据损坏；其余自检（不合格带 ID / ID 形状非法 / 跨版本）照常适用。豁免以**容器标签**判定，不做字段特判。
- 登记簿只显示数量与状态，不含任何问题内容（既有结构即如此：计数 + broken_refs 摘要，本批不破坏）。

### 4.2 索引 `rebuild_evidence_index`

- `_reg` 扫描同样扩展两容器；span key 格式不变（`dv|clause_id|s|e`）；同 span 多 ID 防御逻辑照旧。
- **失效降级**：不合格票（verification 不符 / 缺坐标 / 空 ID）→ 沿用既有行为跳过，同时登记簿 broken_refs 可见——索引与登记簿对坏票的说法一致：一个不收，一个记账。

### 4.3 读路径不归一化 ask 账本

- 账本票据在**写入时**已按 §3.3-4 规范化（normalize 复核通过才入账），存的就是终态；GET 读路径对 ask_evidence **不做归一化**（GET 永不写 store 红线），登记簿/索引按存量直接派生。
- 旧行无 ask_evidence → 零改动（容器不存在即跳过）。

## 5. 三路并发测试（必须能制造真实写入冲突）

### 5.1 写路径纪律（被测契约）

Ask 的 读→追加→写回→重建索引 与 verify confirm / objection adopt 的读改写，**全部** `verify_service.lock_for(review_id)` 内完成——任何一路绕锁即违约。

### 5.2 冲突制造法（「同时启动几个线程」不够——必须强制陈旧读）

- **T-C1（真实冲突）**：两线程用 Barrier 对齐起跑，且**都先完成读**（注入受控延迟/事件，强制两线程都拿到旧快照）后，一线程写 Ask、一线程写 verify（或 objection adopt）→ 断言**两笔写入都存活**（无 lost update）+ evidence_index 与最终行内容一致。
- **变异验证（本测试的咬合力证明）**：
  - 把 `lock_for` 替换为 no-op（或提前释放）→ T-C1 **必须红**（陈旧快照互相覆盖，丢一笔）；
  - 恢复锁 → 重新绿。
  - 若删锁后测试仍绿，说明测试没抓住真冲突——测试本身判不合格，回炉。
- **T-C2**：Ask 入库与 GET 读并发 → 读幂等、store 无撕裂。
- 沿用 conftest 槽位排空纪律，防 429 污染并发判定。

## 6. 测试清单与变异验证（红线：回滚必须变红）

| 编号 | 钉子 | 变异 |
|---|---|---|
| T-A1 | absent：声明 → status=absent、无票无边 | absent 判定改恒 missing → 红 |
| T-A2 | present：有票有 ID，且 `counter_evidence_ref` **四接线全通**（归一化/登记簿/索引/标注边） | 拆任一接线 → 红 |
| T-A3 | missing：定位失败 → status=missing、ref 为 None、**无 ID** | 伪造空 ID/伪 ID → 红 |
| T-A4 | 未受理 → status 空串、**不得标 missing**、ref None | 未受理标 missing → 红 |
| T-A5 | missing/absent 不碰 accepted / rebuts_status / claim_id / content_hash | 任一联动 → 红 |
| T-A6 | 旧记录兼容：新字段缺席 → 空串/None、响应除新键外逐字节一致 | 回填推断 → 红 |
| T-A7 | counter 边资格校验含 document_version 严格相等（跨合同票无边） | 去掉 dv 校验 → 红 |
| T-B1 | ask 成功入账：五键白名单、answer/raw_text 零字节落库、question >200 截断 | 回答入账 → 红 |
| T-B2 | 门禁四条：空 ID / unverified / 跨版本 / normalize 复核不过 → 不入账且响应正常 | 放行任一 → 红 |
| T-B3 | TTL=0 不登记 | 改为登记 → 红 |
| T-B4 | ask_evidence_count 派生正确；明细与问题内容不下发 | 下发 → 红 |
| T-B5 | Ask 票进登记簿（计数/unique/multi_source）**且进索引**；quote 自检豁免生效（Ask 票不因缺 quote 记 broken） | 拆接线/去豁免（Ask 票被误记 broken）→ 红 |
| T-B6 | 上限 50：超限不再入账、响应正常、env 非法值启动报错 | 静默容错/负数接受 → 红 |
| T-C1/C2 | 真实冲突并发（§5.2，含删锁变红变异） | 删锁仍绿 = 测试不合格回炉 |
| T-D1 | AskResponse schema 不变（旧客户端兼容）；counter_evidence_ref 往返不被剥 | 剥既有键/剥新字段 → 红 |

## 7. 明确不做

- Ask 回答全文 / 对话回放 / 会话持久化（永久红线，不因本批松动）
- citation_edges / decision_history / cited_by 视图（**后置冻结**，roadmap 6.1.8——v1.3 设计稿战略调整同步）
- pending 稳定对象键（延后；pending 问题照旧不发正式编号）
- 前端计数展示 UI（字段先到，展示随 2c 报告闭环）
- Ask 层 claim_id / 决定链（§3.6 防走样）

## 8. Q1-Q4 裁决记录（审计 2026-09-24，全部落定）

| # | 问题 | 裁决 |
|---|---|---|
| Q1 | 未受理异议是否建反证票？ | **不建**（通过；状态记空串，见 §2.1 四态表） |
| Q2 | 旧记录空状态是否进迁移警告？ | **不进**（通过；counter 状态非身份链事件） |
| Q3 | ask_evidence 每 row 上限？ | **默认 50，环境变量严格校验**（负数/非法值启动即报错，照 store.py fail-fast 先例） |
| Q4 | llm_ask.py:302-322 重复建票是否删除？ | **删除**（通过；同文件同函数、行为等价，T-B1 覆盖） |

## 9. v1.0 → v1.1 对账（审计退回六项逐一闭环）

| 审计要求 | 落实 |
|---|---|
| 1 反证票据实际存储字段 | §2.2：`counter_evidence_ref` 双层模型 + 六接线点清单（服务/API/归一化/登记簿/索引/标注） |
| 2 Ask 票进登记簿与索引完整路径 | §4 全节：计数参与口径、span key、失效降级、quote 自检豁免、读路径不归一化 |
| 3 Ask 内部票据字段白名单 | §3.2：七键逐一列名，**不存 quote** |
| 4 Ask 无效票据不入账规则 | §3.3：门禁四条（ID 非空/资格态/dv 严格相等/normalize 复核），不合格照常回答不入账 |
| 5 未受理异议的状态规则 | §2.1 四态表：未受理 = 空串，不得标 missing；missing 四不碰（§2.1） |
| 6 真实写入冲突的并发测试 | §5：陈旧读强制注入 + 删锁变红变异；删锁仍绿 = 测试回炉 |

## 10. 施工放行钉子（外审 ChatGPT 2026-09-25「有条件放行」四条，施工时必须钉死）

> 外审判词：v1.1 设计通过，有条件放行进入代码施工。四条钉子不改设计，直接落进代码与测试。施工顺序：写代码 → 回归测试 → 删锁变异必须变红 → 恢复代码重新变绿 → CI 通过 → 独立验收。不扩展新账本、新关系类型或未来治理功能（治理冻结，roadmap 6.1.8）。

| # | 钉子 | 落实方式 |
|---|---|---|
| 1 | 「未发现反证」判定先统一格式 | AI 申报文案先归一（strip + 全角空白折叠 + 已知表述集匹配）再判 absent/missing；**不得因空格/措辞差异把 absent 误判 missing**。测试钉：同义表述集（含前后空格、全角空白）判 absent 结果一致 |
| 2 | `asked_at` 必须服务端产生 | 写入时取服务器时钟，忽略模型/客户端提供的任何时间值。测试钉：伪造 asked_at 的票据入账后时间 = 服务器时间 |
| 3 | Ask 票据归属合同再确认（实测化） | §3.3 门禁三条各配对抗测试：A 合同票据入 B 合同行必须被拒；document_version 不一致必须被拒；坐标/normalize 复核不合格必须被拒 |
| 4 | `ask_evidence_count` 只计合格票 | 计数按 §3.3 门禁通过的记录统计；坏记录（缺键/资格态不符）**不入计数**（宁少勿多，fail-closed 口径）。测试钉：混入坏票后计数不变 |
