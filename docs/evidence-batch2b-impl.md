# 证据法批 2b 实现稿：Claim 标识 + 带关系类型的引用边

> 状态：**v1.3——终审通过 2b-①，实施中**（2026-09-20）。终审放行范围：仅证据地基（规范化/span 复用/evidence_index）；claim_id 与 counter/Ask 等 2b-②③ 待 2b-① 验收后再动。终审两条施工纪律与 canonical 收敛设计决策见 §1.9。
> 前置：批 2a 已正式验收通过（基线 cefd787）。节奏：先审设计、再写代码；本稿通过后按修订后的三刀顺序开工。

## 0. 范围一句话

让每条「主张」（规则命中 / 补盲候选 / 质量观察 / 核验疑点 / 异议）拥有稳定 `claim_id`，并携带**带关系类型的证据引用边** `evidence_refs`——把「各自抄原文」升级为「引用同一张身份证」。

## 0.1 施工顺序（v1.1 重排，审计修订一）

```
2b-① 证据规范化与 span 复用（地基：先把身份证定稿）
      └─ resolve_or_build_evidence 收口
      └─ evidence_index（可重建缓存）
      └─ 并发与幂等测试

2b-② Claim ID / evidence_refs / citation_edges（主张编号：身份证定稿后才发编号）
      └─ 稳定业务主键（禁下标）
      └─ claim_content_hash / claim_revision
      └─ API schema 与旧记录兼容

2b-③ counter_evidence 与 Ask 入库（反证与追问：missing/absent 分离）
      └─ 隐私 / TTL / 50 条上限 / 不存回答全文
```

**为什么必须重排（审计修订一，已接受）**：v1 让 2b-① 先发 claim_id、2b-② 再做证据复用——相当于「先给文件编号，后来又把文件换了」：复用若改变 evidence_id，已发的 claim_id 就成了旧编号。改为证据复用落地后才生成 claim_id，且 2b-① 期间**禁止持久化任何 claim_id**。

## 1. 审计五重点逐一设计（v1.1 修订版）

### 1.1 claim_id 是否真正稳定（审计重点一 + 修订二/三）

```
claim_id = "cl-" + sha256(document_version + "\x1f" + claim_type + "\x1f" + 业务主键)[:12]
```

**业务主键（v1.1 重设计——禁下标、禁截断文本、禁模型输出顺序）**：

| claim_type | 业务主键 | 下标禁令说明 |
|---|---|---|
| rule_item | primary evidence_id（合格票） | — |
| blind_candidate | 库内稳定 id + primary evidence_id | — |
| quality_observation | dimension + primary evidence_id | 不用「第几条观察」；同 dimension+同证据的两条观察视为同一主张（合并规则见 1.7，第二轮钉 3） |
| verify_question | source + **primary evidence_id** | **v1.1 变更**：v1 曾引用 source_ref——实测 verify.py:386 的 `source_ref=f"obs:{i}"` 正是数组下标身份（审计修订二实证准确），source_ref 降级为纯展示参考，永不入身份 |
| objection | item_id + direction + primary evidence_id | — |

**无有效主证据的主张（审计修订三，已接受）**：主证据定位失败 → primary evidence_id 为空 → **不发正式 claim_id**（claim_id 字段留空），该主张只参与展示不参与关联账目；绝不用空字符串参与身份计算（否则多个「无证据」主张会撞出同一个假编号）。这类主张在登记簿 broken 账目可见（2a 自检已覆盖）。

**内容漂移防护（审计修订三，已接受）**：claim_id 只是「档案编号」，不保证编号下的说明文字不变。新增：

- `claim_content_hash`：主张说明文本（question/comment/note 等展示字段拼接）的 sha256[:12]——读路径校验，内容变了 hash 变，2c 的决定记录可以据此发现「编号没变但内容被偷换」
- `claim_revision`：整数，本批恒 0，契约预留（内容变更通道属规则修订体系，批 3）
- `supersedes_claim_id`：同 v1.1 设计稿 §4.2，本批不填值

**稳定性测试（新增，审计点名）**：同一批观察**仅调换顺序** → 全部 claim_id 不变；改一个字 → content_hash 变而 claim_id 不变；同一主张两次生成 → 同 claim_id。

### 1.2 relation 五类如何分得清（审计重点二）

```python
relation = Literal["primary", "supports", "rebuts", "context", "counter"]
```

| relation | 语义 | 2b 实际产生场景 | 判定方 |
|---|---|---|---|
| primary | 主证据 | 每条主张的内嵌合格票据 | 服务端（结构性） |
| supports | 同主张的辅助引用 | **2b 不实际产生（第二轮钉 2）**——当前代码质量观察与事实之间没有 fact_id 或服务端关联键，程序无法可靠判定「哪条事实支持哪条观察」，让程序猜 = 造关系。枚举保留，待事实-观察关联机制（fact_id）落地后再启用 | 服务端（有真实来源后） |
| rebuts | 被反驳主张的证据 | 异议 → 所争议规则项（**仅受理条目**，Q4 裁决） | 服务端（结构性） |
| counter | 反证 | counter_evidence 票据化（见 1.5） | 服务端（定位成功才发） |
| context | 背景引用 | 本批预留枚举，不产生 | — |

- **分工红线**：模型只产 quote 和主张内容；relation 全部由服务端按结构位置判定，模型输出的任何关系声明一律忽略。
- **evidence_refs 只放真正合格的证据（审计修订五，已接受）**：`evidence_refs` 中每个元素必须 `evidence_id` 非空且 verification 合格——**空 ID 不伪装成正式引用**。

### 1.3 服务端如何复用同一 span（审计重点三 + Q2 裁决）

```
层产 quote → 服务端 locate_quote_span(全文) → 规范 span (start, end, clause_id)
  → 查 evidence_index（同 document_version）：
     命中（version + clause + span 完全相等）→ 复用既有票据
     未命中 → 新建票据 + 登记索引
```

- **Q2 裁决落地（完全相等才复用，不引入模糊阈值）**+ 三个必测场景：
  1. 空格/换行/标点不同但压缩匹配定位到**同一真实 span** → 复用同一 ID
  2. 真实坐标不同 → 不复用
  3. 复用出的 start/end 必须是 locate 的精确产物，**不允许估算端点**（复用票的坐标以首张票为准，后续层只引用不重算）

### 1.4 evidence_refs 与 citation_edges 的分工（审计修订四，v1.1 明确）

```
evidence_refs = 当前主张「现在」引用什么   —— 主张对象上的当前状态，可从内嵌票据派生重建
citation_edges = 历史上「曾经」发生过哪些引用 —— 只追加历史账目，永不重建
```

- **v1.1 收缩范围**：2b 只落地 `evidence_refs`（当前状态，随主张对象存在，读路径可派生补齐旧记录）；**citation_edges 整体推迟到 2c**，与 decision_history 同批落地（二者同属「只追加历史账本」，共享写入与治理规则）。
- 这样避免了 v1 的矛盾（「当前引用是派生的，历史又声称不可重建」）：2b 内只有一种引用形态（当前状态、可派生），历史账本的持久化语义在 2c 连同决定记录一起设计。
- citation_edges 的预定义契约（2c 实施，此处只登记不实现）：存 row["citation_edges"]、元素含 claim_id/evidence_id/relation/at、只追加、去重键=(claim_id, evidence_id, relation)、失效标 broken 不删、读路径禁改。

### 1.5 counter_evidence 与空引用语义（审计重点四 + 修订五）

- 反证流程：模型产 counter_evidence 文本 → 服务端 locate → 定位成功 → 建票 → `relation=counter` 进 evidence_refs；**定位失败 → 不进 evidence_refs**，进单独的异常记录：

```json
// objection 对象上的异常记录（不与正式引用混放）
"counter_evidence_status": "missing",   // absent=模型没提供反证原文 | missing=提供了但定位不到
"counter_evidence_reason": "not_located",
"counter_evidence_ref": { ...verification=missing 的票据, evidence_id="" }  // 透明留痕，无资格
```

- **absent / missing 分离（审计修订五，已接受）**：模型没给反证原文 = `absent`（不是证据问题，是没主张反证）；给了但定位不到 = `missing`（证据资格问题）。二者文案、账目、统计分开。
- **空 ID 铁律**：空 evidence_id 的引用**永不参与**统计、报告、决定（2c 决定记录引用 counter 时必须校验 ID 合格）。
- **只加不删**：counter_evidence 裸字符串字段保留原样；`counter_evidence_ref` 票据照建（verification=missing，与批 1「missing 不发 ID」一致）。

### 1.6 evidence_index 按「缓存」治理（审计修订六 + Q3 裁决）

> evidence_index 是**为了查重而存在的可丢弃缓存，不是证据事实本身**。2a 登记簿保持纯读零写，两者并存不冲突（一个是读视图，一个是写路径的临时加速结构）。

治理规则（全部落实）：

1. **只存坐标和 ID，且方向按查重需求组织（第二轮钉 1）**：查重是「拿位置找票」，因此主键必须是 span：

```json
{
  "version": 1,
  "by_span": {
    "document_version|clause_id|start|end": "ev-..."
  }
}
```

   可另留 `by_id`（ID 反查坐标，供引用边校验）作辅助，**但不能只有 ID 反查坐标**——那样每次查重都要全量扫缓存，等于没有索引。**不保存合同原文/摘句**（票据里有，缓存不需要）
2. **带版本号**：`evidence_index.version`，契约演进 +1
3. **可重建**：损坏/缺失时从六容器票据全量重建；读路径发现索引与票据不一致时，**只修正响应副本，绝不写回数据库**（审计修订六红线）
4. **写路径合并防覆盖（审计修订六，已接受）**：`store.update` 有锁但「先读→再改→再写」全过程可互相覆盖——规则：**所有 done 后的写方（ask/verify/objection）必须在 `lock_for(review_id)` 锁内重取行→合并索引→写回**；pipeline 阶段单 worker 无并发。必测：Ask、Verify、Objection 同时写入 → 索引与引用边零丢失
5. **TTL**：随行过期，无独立生命周期

### 1.7 同一证据对应多个主张的合并规则（第二轮钉 3）

`dimension + primary evidence_id` 相同的多条质量观察 = 同一主张的多个「表述」。合并规则（全部与输入数组顺序无关）：

| 项 | 规则 |
|---|---|
| 展示 | 多条观察**共存不删除**（页面照常逐条显示，删用户可见内容超出本批授权） |
| 主张身份 | 多条观察写入**同一个 claim_id**（它们是同一主张的多个说法） |
| 规范文本 | 取全部说明文本的**字典序最小者**（确定性、与输入顺序无关；不选「第一条」不选「最长」——两者都依赖顺序或长度这种偶然属性） |
| claim_content_hash | 对全部说明文本**排序后拼接**再哈希——任何一条表述变化都能被 2c 决定记录察觉 |
| evidence_refs | 按 `(evidence_id, relation)` 去重，并按 `(relation, evidence_id)` **固定排序**——同一主张无论由哪条观察派生，refs 逐字节一致 |
| 其他主张类型 | 天然唯一（业务主键已含 item_id/source_ref 替代键等），无需合并；若未来出现同键冲突，按本表字典序规则处理并在 PR 说明 |

### 1.8 压缩定位的端点必须真实坐标（第二轮钉 4，2b-① 必修）

**现状核实（审计指出，已确认）**：`app/services/evidence.py` 的 `locate_quote_span` 在空格/换行压缩匹配成功后，`end` 仍按摘句长度估算（`s + len(bare)`，估算路径 `text[s:end] != bare`）——与本稿「坐标必须真实」冲突。

2b-① 一并修掉：

- 压缩匹配路径的 `end` 改为**向后扫描真实非空白字符**对齐到压缩后摘句的等价区间——保证 `text[start:end]` 去除空白后 == 规范化摘句
- **硬测试**：对全部压缩匹配样例断言 `text[start:end]` 即真实原文区间（非估算）
- **ID 迁移影响面（如实声明）**：此修复会改变压缩匹配路径既有票据的 end → evidence_id 随之重算。影响被批 1 的读路径归一化吸收（旧票重算 ID，`id_recomputed` 警告可见）——正是 2a broken 账目的用武之地，无需数据迁移
- exact 匹配路径不受影响（end 本就精确）

### 1.9 终审施工纪律与 canonical 收敛决策（v1.3 实施记录）

**纪律 1（同 span 异 ID 固定收敛）**：resolve_or_build 与登记簿自检双处落实——同 span 出现多个 ID 时固定选**字典序最小合法 ID**并记 `duplicate_span` 警告；双向确定性有测试钉（既有更小→保留既有；更大→新票胜出）。

**纪律 2（老票据迁移回归链）**：已实现为测试 `test_legacy_estimated_end_migrated_on_normalize`：旧估算票 → 读路径纠正真实坐标 → ID 重算 → 精确票据零改动 → 全错票 fail-closed 降级。

**canonical 收敛设计决策（span 复用的实质机制）**：合格票的 `quote` 统一取 `text[start:end]` 原文切片（截 300）——同一 span 的任何措辞变体（首尾空白/句读装饰/截断差异）在归一化时收敛到**同一 ID**（哈希的 quote 项一致）。收益：①七层无需逐调用点穿索引线（收敛集中在 evidence.py 单点）②索引/缓存无需保存合同原文（切片随时可从 text 重建，满足审计「缓存只存坐标和 ID」）③`_QUOTE_TRIM` 补中文句读（。，、；！？）使带句号/不带的变体也收敛。影响面如实声明：落库前 pipeline 统一规范化——存进 store 的即 canonical 形态，读路径幂等零改写；老票据 ID 变化由 id_recomputed 警告记账（无迁移脚本）。

**2b-① 范围口径**：resolve_or_build_evidence / rebuild_evidence_index / 索引随行持久化（pipeline 落库 + verify 端点锁内合并）全部交付；七层调用点逐个换线**不在本刀**——canonical 收敛使各层现有 build 调用天然收敛，逐点换线的增量价值（写时 O(1) 查重）从 2b-②（claim_id 持久化依赖写时身份）起才有意义，届时随 claim_id 落地。

## 2. 分批实施（v1.1 重排后）

| 小批 | 内容 | 载荷 |
|---|---|---|
| **2b-①**（实际交付口径，审计 PR review P2 修订）| 坐标规范化（locate 真实端点 + 归一化坐标校验/端点归一）+ canonical 收敛（quote=原文切片）+ resolve_or_build_evidence / rebuild_evidence_index 机制 + evidence_index 随行持久化（pipeline 落库 + verify 端点锁内合并）+ 登记簿 duplicate 自检 + 四组测试。**七层调用点逐点换线不在本刀**（见 §1.9 范围口径）——各层现有 build 调用经读路径/落库规范化天然收敛 | evidence.py、pipeline.py（落库规范化+索引）、routes_verify.py（锁内合并）、routes.py（索引透传）、测试 |
| **2b-②** | claim_id + claim_content_hash + evidence_refs（primary/supports/rebuts）+ API schema + 旧记录兼容 | evidence.py 派生函数、各层产出点、normalize 补齐、schemas、顺序稳定性测试 |
| **2b-③** | counter_evidence 票据化（absent/missing 分离）+ rebuts 边（仅受理）+ Ask 引用入库 | objection.py、llm_ask.py、测试 |

## 3. 测试策略（v1.1 增补）

1. 变异验证：回滚引用逻辑必须变红（2a 六组教训全数沿用）
2. **顺序稳定性（审计修订二）**：同批观察换序 → claim_id 不变；同容器多条目同证据 → 合并语义明确
3. **复用三场景（Q2）**：措辞异/位置同 → 同 ID；位置异 → 不同 ID；坐标精确非估算
3a. **真实坐标硬测试（钉 4）**：压缩匹配样例断言 `text[start:end]` == 真实原文区间
3b. **合并确定性（钉 3）**：观察换序 → 规范文本/refs/content_hash 全部不变；refs 按 (evidence_id, relation) 去重定序
4. **无主证据主张（修订三）**：claim_id 留空不撞号；broken 账目可见
5. **空引用铁律（修订五）**：evidence_refs 无空 ID 元素；absent/missing 分离各有断言
6. **并发零丢失（修订六）**：ask+verify+objection 并发写 → 索引与边零丢失；读路径只改响应副本（对照 store 行字节）
7. 铁律 3：claim/refs 全是主张侧字段，档位路径零触碰
8. 兼容：旧记录无新键 → 读路径补齐行为不变；counter_evidence 裸字符串保留

## 4. 宪法 impact

- 无条款变更。铁律 3 重申 + 新增测试钉（2b-② diff 内 items[].status 逐字节对照）
- **口径纪律（Q4 裁决）**：`accepted` = 「准许进入复核」，不是「人工采纳」、更不是「异议成立」——文案与字段注释统一此表述；真正的人工采纳是 2c 的 Decision 记录（authority=human）
- Single Evidence Fact（第十六条）：2b-① 落地生成侧真复用

## 5. v1 → v1.1 修订记录（审计六处对账）

| 审计意见 | 落实 |
|---|---|
| 修订一 先规范证据后发编号 | §0.1 重排三刀；2b-① 期间禁持久化 claim_id |
| 修订二 禁下标身份 | §1.1 业务主键重设计；source_ref 实证（verify.py:386）降级纯展示；换序稳定性测试 |
| 修订三 无主证据主张 + 内容漂移 | §1.1 无合格主证据不发正式 claim_id；claim_content_hash/claim_revision 契约 |
| 修订四 refs/edges 分工 | §1.4 refs=当前可派生，2b 只做 refs；citation_edges 推迟 2c 与 decision_history 同批 |
| 修订五 空引用不伪装 | §1.5 evidence_refs 只收合格票；absent/missing 分离；空 ID 永不入统计报告决定 |
| 修订六 evidence_index 缓存治理 | §1.6 只存坐标 ID、带版本、可重建、读路径只改响应副本、锁内合并防覆盖 + 并发测试 |
| Q1 双路径 | 采纳，修正为「证据规范完成后写路径生成；无主证据不发正式 claim_id」 |
| Q2 完全相等复用 | 采纳，+三必测场景（措辞/位置/坐标精度） |
| Q3 持久化=可重建缓存 | 采纳，§1.6 治理五规则 |
| Q4 rebuts 仅受理 | 采纳，+「accepted≠采纳」口径纪律 |
| 第二轮钉 1 索引方向反了 | §1.6-1：by_span 主键（version\|clause\|start\|end → ev-id），by_id 仅辅助 |
| 第二轮钉 2 supports 无来源 | §1.2：supports 2b 不实际产生（无 fact_id 关联机制，不猜关系） |
| 第二轮钉 3 合并规则不明 | §1.7：共存不删/字典序规范文本/排序拼接 content_hash/refs 去重定序；修正 1.3 引用为 1.7 |
| 第二轮钉 4 压缩 end 估算 | §1.8：2b-① 修 locate_quote_span + 硬测试 + ID 迁移影响面如实声明（归一化吸收） |
