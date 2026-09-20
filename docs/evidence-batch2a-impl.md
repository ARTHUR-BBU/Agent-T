# 证据法批 2a 实现稿：证据登记簿（纯读路径派生视图）

> 状态：**v1.1——审计有条件通过，修订后开工**（2026-09-20）。设计依据：《证据法批 2 设计稿 v1.1》§4.1 / §4.3 / §5-2a。
> 审计裁决：纯读路径派生 ✅ 认可；entries 默认不下发 ✅ 认可（明细在 Claim/Decision，必要时受控审计出口）；
> 两个开工前修订已落实：①broken_refs 在归一化**改写前**捕获（报警器不能先擦报警记录）+ broken_ref_count 全量
> ②occurrence/unique 命名拆分（total 退役）。2a 验收四问：目录是否准确、是否幂等、是否不泄露原文、是否完全不写 store。

## 1. 范围一句话

在 get_review 的归一化循环之后，对归一化行做一次派生遍历，产出「本案卷证据目录」概览，随 ReviewSummary 下发。**零写路径、零 store 变更、零前端变更。**

## 2. 数据契约

```python
# app/services/evidence.py 修订
def normalize_review_evidence(row, warnings: list | None = None) -> dict:
    """批 1 既有函数；2a 加可选 out 参数——归一化**改写票据前**捕获异常。

    审计修订一：报警器不能先擦掉报警记录。票据被归一化清 ID/降级/重算
    之前，把旧状态记进 warnings（不写回数据库，只保留本次读取的发现）。
    """
    # 捕获时机：_fix 改写前记录 (where, old_evidence_id, new_evidence_id, reason)
    # reason ∈ unqualified_id | id_recomputed | downgraded_unlocatable


def build_evidence_registry(row_normalized: dict, warnings: list) -> dict:
    """由归一化行 + 归一化异常派生证据登记簿概览（纯函数，读路径专用）。

    可重建性的最高形态：不持久化——每次响应即时重建，
    store 行零接触（归一化本身已保证深拷贝不突变）。
    """
    return {
        "registry_version": 1,           # 契约版本：未来加 cited_by 边/claim 关联时 +1
        "rebuilt_at": "<ISO8601>",       # 本次重建时间（每次响应即时值，非持久化）
        # 审计修订二：出现数与唯一数必须分开，total 命名退役
        "occurrence_total": N,           # 票据出现次数（含不合格；同票多层各计一次）
        "unique_total": N,               # 唯一 evidence_id 张数（空 ID 出现各计一张，不参与去重）
        "qualified_occurrence_total": N, # 合格票出现次数（verified/ambiguous）
        "qualified_unique_total": N,     # 合格唯一票张数
        "multi_source_unique": N,        # 同一 evidence_id 被 ≥2 层引用的唯一票数
        # 审计修订一：broken 账目 = 归一化前捕获的异常（归一化后已不可见）
        "broken_ref_count": N,           # 本次读取发现的归一化异常总数
        "broken_refs": [                 # 前 20 条样本（超限靠 count 知全量）
            {"where": "items[2].evidence", "old_evidence_id": "ev-old",
             "new_evidence_id": "", "reason": "unqualified_id"},
        ],
    }
```

- **entries 不下发**（裁决确认）：明细在 Claim/Decision（2b/2c），必要时提供受控审计详情出口；2a 只给概览
- **where 格式**：`容器名[下标].evidence`，仅调试辅助（v1.1 §4.3：path 不作身份）
- **broken 的捕获时机（审计修订一核心）**：归一化改写前记录——「报警器不能先擦掉报警记录」。missing+旧 ID 的票据经归一化后 ID 已清空，若不在改写前捕获，登记簿永远看不见曾经的脏数据

## 3. 接线点（唯一）

`app/api/routes.py` get_review：在 `row = normalize_review_evidence(row)` 之后：

```python
warnings: list = []
row = normalize_review_evidence(row, warnings)
evidence_registry = build_evidence_registry(row, warnings)
```

随 ReviewSummary 新字段 `evidence_registry: Optional[EvidenceRegistryInfo] = None` 下发（schemas.py）。

- **只接 get_review，不接 verify 子路由**：登记簿是「案卷目录」级视图，verify 出口是单状态查询（批 1 已归一化），不需要目录
- 前端本批**不消费**（v1.1 §8 明确：界面收敛另批）；schema 先行是批 1 「schema 缺字段静默 ignore」教训的反向应用——先立契约，消费端后续接入

## 4. 实现细节

### 4.1 遍历容器（与 normalize_review_evidence 同清单）

items / blind_candidates / quality.observations / quality.facts / facts / objections.objections / verify.questions ——七处 evidence 字段逐一检查。

### 4.2 multi_source_unique 判定与语义边界（审计修订二 + 限制声明）

按 evidence_id 分组计数（**parse_source 不参与身份**——批 1 已定），同一 ID 出现于 **≥2 个不同容器**即计入多源——按容器名分组（门禁 P2-1 消歧：同容器内多个条目持同 ID 不计多源，保守口径防同层重复膨胀）。

> **2a 语义边界（必须随文档/PR 声明，防过度承诺）**：evidence_id 哈希含 quote 本身，
> 「验收合格后付款」与「验收合格后付款。」即使定位到同一正文位置也是不同 ID。
> 因此 2a 的 multi_source_unique 只统计**现有 evidence_id 的精确一致性**，
> **不宣称完成语义级同证据合并**——后者依赖 2b 的服务端 span 规范化复用（设计稿 §4.6）。

### 4.3 性能

单遍 O(总票据数)，与既有归一化同量级（实测票据 ≤ 几十张/卷）；rebuilt_at 用 `datetime.now(timezone.utc).isoformat()`。无缓存、无锁、无 IO。

### 4.4 宪法 impact

- 铁律 3 / 铁律 5：零触碰（纯读视图，无裁决语义）
- Single Evidence Fact（第十六条）：从「哈希隐式」到「账目可见」的第一步
- 归一化职责边界（v1.1 §4.1）：登记簿派生在归一化**之后**，对 citation_edges/decision_history（2b/2c 才出现）零接触

## 5. 测试清单（tests/test_evidence_registry.py）

| # | 测试 | 断言核心 |
|---|---|---|
| T1 | 多源同票 | 同一 quote 注入 items+quality+verify 三容器 → occurrence_total≥3、multi_source_unique ≥1、qualified_unique 计 1 张（同一 evidence_id） |
| T2 | broken 哨兵（修订一） | 注入「missing 却带 ev-old」票据 → broken_ref_count ≥1，样本含 where/old_evidence_id="ev-old"/reason=unqualified_id——**归一化清 ID 前捕获** |
| T3 | 幂等 | 同一记录连续两次 GET → 除 rebuilt_at 外逐字段相等 |
| T4 | 旧记录兼容 | 批 1 之前形态的行（无 evidence 键）→ 200，registry 各计数为 0 不报错 |
| T5 | 隐私契约 | 响应 JSON 序列化后不含任何 quote 全文（断言无票据 quote 字段泄漏进 registry） |
| T6 | 正常审查冒烟 | 上传 fixture → status done → qualified_unique_total ≥1 且 occurrence_total == 带票据条目数 |
| T7 | 响应体积护栏 | 注入 25 条脏票 → broken_ref_count==25 而 broken_refs 样本 ≤20 条（超限靠 count 知全量） |
| T8 | schema 契约 | EvidenceRegistryInfo 字段齐备（含 occurrence/unique 四计数拆分）；旧客户端忽略新字段不受影响 |
| T9 | 无 warning 路径 | 干净记录（归一化零改写）→ broken_ref_count==0 且 broken_refs==[] |

## 6. 变异验证计划（红线：回滚必须变红）

| 变异 | 预期变红的测试 |
|---|---|
| M1：删掉 multi_source 分组计数 | T1 |
| M2：删掉 broken_refs 判定分支 | T2 |
| M3：遍历漏掉 verify.questions 容器（复刻批 1 六容器教训） | T1（三容器注入含 verify）+ T6 |
| M4：registry 把 quote 全文放进 entries 下发 | T5 |
| M5：归一化不捕获 warnings（审计修订一回滚） | T2（票据被清 ID 后无痕，断言 broken_ref_count≥1 必败） |

每个变异独立 commit 外的手工实验（工作区临时改 + 还原），结果记录进 PR 描述。

## 7. 兼容与回滚

- 新字段 None 语义 = 旧记录/异常路径静默降级，前端与 docx 零感知
- 独立 revert：单 commit 载荷（evidence.py 新增派生/捕获逻辑、routes.py 约 8 行接线、schemas.py +2 模型、tests +1 文件），revert 无耦合

## 8. 明确不做（2a）

- store 行持久化 / 写路径接线（纯视图；若审计要求持久化，需先裁决「归一化不突变 store 行」保证的豁免口径）
- cited_by 边 / claim_id（2b）
- decision（2c）
- 前端消费、docx 消费（2c）
- 明细 entries 下发（隐私+体积）

## 9. 审计确认记录（2026-09-20）

1. **纯读路径派生**：✅ 认可（「数据库是档案原件，登记簿是打开档案时临时生成的目录」）——但明确：纯读派生只生成「当前目录」，不替代 2c 的「历史决定账本」
2. **entries 默认不下发**：✅ 认可——2b/2c 必须保证 Claim 可见 evidence_refs、Decision 可追 claim_id/evidence_id，明细由服务端按 ID 查询，不让用户只见数量不见内容
