# B2-3 批2 影子校准报告 · Jev 第一轮（2026-10-17）

> 冻结基线：split_frozen=2026-10-16（MANIFEST）；提示词哈希见各 jsonl 的 prompt_hash；
> 留痕=spec-b2 硬接口 4 八字段（jsonl 全量）。设计稿 spec-b23-classification-gold.md v1.2。

## 成绩总览（TypeSafe Jev，jev-latest）

| 集合 | 成绩 | 说明 |
|---|---|---|
| 校准集（22 份） | **17/22** | 5 MISS 全部判 other、置信 0.71-0.99（见下节） |
| 留出集（7 份） | **7/7** | 含住建部 5.2 万字边界探针→consulting ✓（截断版，全文复核待 GLM 通道） |
| 标题消融探针（2 份） | 见下 | tisvc 抹标题仍对；techdev 抹标题更坚定 other |
| 合计 | 24/29 | 公开示范文本来源，**不得表述为真实合同泛化准确率**（红线） |

> **截断口径脚注（肉饼门禁 P2-1）**：校准 22 份中 14 份、留出 7 份中 3 份为截断输入。本批跑分用的是初版策略（head_6000 only，coverage 字段=truncated_head_6000）；入库脚本现行策略为 head_6000+tail_2000（§5 协议，coverage=truncated_head_6000_tail_2000）——两代策略不同代，用入库脚本无法逐字节重放 080504 批次。挂账：GLM 通道恢复后对截断样本按现行策略重跑复核。
> **合计口径注记（P3-1）**：对外引用统一校准集口径（17/22→22/22）；合计 24/29 系校准+留出混算，留出集不含 other 判别力。
> **GLM 补跑留痕（P3-4）**：2026-10-17 已试补跑一次仍 503（shadow_glm_calibration_20261017_081853.jsonl 降级留痕），通道未恢复。
> **relabelled 字段口径**：shadow_jev_calibration_20261017_080504_relabelled.jsonl 中 gold_label 保留修订前原值（历史留痕），小智娘 P2-1 修正：correct 保持原始语义（pred==gold_label，翻格五行为 false），22/22 由 revised_correct 字段计算——全库不变式 correct==(pred==gold_label) 恢复。

## 5 个 MISS = 高价值混淆信号（矛头指向金标标签，非模型）

校准集 5 个 MISS 的预测都是 `other` 且置信 0.87-0.99——模型在说「这些合同不属于我认识的八类」，逐份验尸（0.71 系 techdev 抹标题消融版，见下文探针节，非校准集 MISS）：

| 样本 | 金标标签 | 模型判 | 验尸结论 |
|---|---|---|---|
| mandate-contract-samr-2025 | procurement | other 0.87 | **标签疑误**：委托合同不是货物买卖（procurement 定义=买卖/采购/供货） |
| construction-work-contract-2017 | procurement | other 0.99 | **标签疑误**：建设工程施工合同在八类集合中无家（施工≠货物买卖） |
| work-contract-gf-2000 | procurement | other 0.97 | **标签疑误**：承揽/加工合同八类无家 |
| techdev-coop-most-2001 | technical_service | other 0.98 | **标签疑误**：技术开发≠技术服务（民法典两类）；产品决策点①只收 4 类（技术服务不含技术开发） |
| techdev-most-2001 | technical_service | other 0.89 | 同上；抹标题消融后 other 0.71（义务结构也指向非技术服务） |

**佐证**：用户决策点①（2026-10-10）原文=「技术服务 4 类收，纯咨询/劳务派遣/技术转让拒收」——四收类没有技术开发；corpus-tisvc 的 techdev 两份当初是按「技术服务近邻」做规则校准用的，在**分类金标层面**其法务标签应为 other（或由老钱另裁）。

**对 17 个「对」的冷思考**：tisvc 3 份空白模板判对有标题助力，但消融探针显示义务结构（技术服务报酬/专项技术服务）独立成立，非纯标题捷径。

## 附件金标

未启动（按设计稿 §6 随真实样本到位后单独建）。

## 成本与通道留痕

- Jev 全量（29 份+消融 2）：约 15 万 input tokens，$5 硬上限内（试射→放量协议已走：3 份试射核 usage 后放量）。
- GLM-4-Flash：**未跑**——easyrouter 中转「限时特价通道」整条 503（glm-5.3-flash 与 mimo-v2.6-flash 均无可用渠道，model_not_found 实锤）。恢复后按同一协议补跑，jsonl 降级字段已记录（shadow_glm_calibration_20261017_080324.jsonl）。

> **〔2026-10-17 定性变更（用户澄清）〕** 503 非临时故障：所购套餐为 coding 套餐，
> 不支持此类直接 API 调用——**easyrouter GLM 通道就此排除，无「恢复」可言**。
> 双模型对比的 GLM 侧取消；后续分诊台生产主通道选型（官方智谱免费 glm-4-flash /
> 生产现用 DeepSeek 兼任 / Jev 转正）待用户拍板。补考计划作废。
- 住建部 5.2 万字：本次走截断版（头 6000）判对；设计稿 §9-3 要求的全文复核随 GLM 通道恢复一并做。

## 待老钱裁定（金标标签修订，签认前不改 GOLD）

1. mandate/construction-work/work-contract 三份：标签 procurement→other？（八类无家=other 正当；改后支持类校准集 procurement 剩 6 份）
2. techdev 两份：标签 technical_service→other？（技术开发非四收类；或老钱认为产品应把技术开发并入技术服务收口——那是品类边界变更，另立议题）
3. 若①②均改：金标 GOLD 修订=5 格翻格（全部→other），成绩重算口径=按修订后标签重放 jsonl（模型调用不重跑，只改对账标签）——签名后生效。

---

## 老钱五格翻格签认（2026-10-17，金标纠错）

**成绩并列呈现（红线：禁止单独引用 22/22）**：

| 口径 | 校准集成绩 | 性质 |
|---|---|---|
| 修订前（金标疑误） | **17/22** | 5 格金标标签疑误，模型判 other 为法务确认为对 |
| 修订后（金标纠错后） | **22/22** | 按修订标签重放既有 jsonl（模型未重跑）；**100% 是纠错后成绩，不是模型变好了** |

留出集 7/7 不变（不含 other 维度——留出集对 other 类零判别力，局限挂账）。

**翻格理由逐份入档（性质+法条+收类定义；模型置信度仅系触发复核信号，不作标签依据）**：

1. **mandate-contract-samr-2025**：procurement→other。委托合同给付为事务处理（民法典 §919），非货物买卖（§595，procurement 收类定义），八类无家。
2. **construction-work-contract-2017**：procurement→other。建设工程施工合同标的为工程成果（§788，准用承揽 §808）；含材料采购条款不改变主给付义务定性。
3. **work-contract-gf-2000**：procurement→other。承揽（加工/定作等，§770）给付为工作成果；定作人供材不改变性质。
4. **techdev-coop-most-2001**：technical_service→other。技术开发与技术服务系 §843 下并列类型（§851 vs §878），给付性质不同（研发成果不确定性 vs 现有技术解决特定问题）；产品决策点①（2026-10-10）四收类不含技术开发，分类层归 other 交分诊台。
5. **techdev-most-2001**：同上；抹标题消融后义务结构仍指向非技术服务（other 0.71）。

**其他签认条款落地**：⑥重算口径=重放 jsonl（shadow_jev_calibration_20261017_080504_relabelled.jsonl）⑦留出集 other 零判别力局限挂账+other 样本量不再追加 ⑧技术开发并入/新增品类=品类边界变更议题需用户拍板，不在金标层面处理 ⑨边界样本两份维持排除另行裁定 ⑩混同类合同（开发+服务混合）按主给付义务定性——口径已写入设计稿 §10。

## 勘误与复现性声明（小智娘门禁 P2-2/P3，2026-10-17）

1. **Jev 提示词哈希勘误**：校准/试射/留出/relabelled 四份 jsonl 的 prompt_hash=13dcc040c784 系修复前误记（GLM PROMPT 顶包）。经 git b2eb331 提交逐字节比对：修复前后实际发送给 Jev 的 instructions+criteria 完全一致——**成绩数据有效，仅哈希留痕失真**。各记录已补 prompt_hash_corrected=478819368c72+erratum 说明（verdict 未动）。
2. **截断整批声明**：080504 校准批与 080601 留出批全部为修复前截断策略（head_6000 无尾）；入库脚本现行策略 head+tail 系门禁整改产物，两代不同代（重跑复核挂账见上节脚注）。
3. **消融探针复现**：shadow_jev_ablation_20261017.jsonl 已补 input_text_synth 字段（抹标题合成输入全文留痕），无需代码即可复现。
