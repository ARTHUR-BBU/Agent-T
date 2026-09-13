# 质量层专项 spec（阶段 2.1，2026-09-12）

> 权威路线：docs/roadmap-llm-ui.md 阶段 2。本文档是质量层的实施 spec 与运行契约。

## 一、定位与铁律

质量层 = 规则清单之外的 AI 参考分析——**参谋不是裁判**。

**铁律 5**（路线图 LLM 定位宪章）：
- 质量层条目不计入评分卡、永不改变规则档位（档位唯由规则引擎签发）；
- 每条必须带合同原文连续摘录（quote）与「待人工确认」标记；
- 结构保证：quality 输出只进 store 的 `quality` 键，永不回流 items/scorecard
  （`test_quality_api.py::test_iron_rule_5_*` 用开/关两次审查深度相等硬断言钉死）。

## 二、三维度定义

| dimension | 中文 | 观察什么 |
|---|---|---|
| completeness | 完整性 | 应有而缺失、或表述空悬无法执行的安排（与规则「未找到」互补，只观察不判档） |
| consistency | 一致性 | 条款之间互相矛盾（付款 vs 验收、期限 vs 违约责任、定义 vs 使用） |
| impact | 影响 | 条款写得通，但按用户立场通读会吃亏的实际后果 |

## 三、四段式规范（2.2 共用）

四段式 = 原文依据 / 实际影响 / 修改建议 / 待确认的事。两处落地：

**质量层条目（批 1）**：
- **原文依据** = `quote`（服务端已对全文校验的连续摘录）；
- **实际影响 + 修改建议** = `comment`（system prompt 强制「前半句影响、后半句建议」两句结构）；
- **待确认信息** = `needs_confirm` 徽章（代码强制 True）+ 容器级 `disclaimer`。

**追问页（批 2，2026-09-13 落地，呈现层映射——后端 OUTPUT_FIELDS 七键不动）**：
- 段① 原文依据 ←「原文在哪」（caption 灰字一行；钉顶原文卡已承载摘句）；
- 段② 实际影响 ←「问题是啥」（正文黑主权重；raw_text 解析失败 <pre> 兜底挂此段）；
- 段③ 建议改法 ←「建议怎么改」+ 改写稿块（复制/溯源/核对提示原样）；
- 段④ 待确认的事 ← 当前键无内容源，缺段静默隐藏；
- 「这条在查啥」不上 UI；「风险等级」不上 UI（**「不伪造风险等级」设计禁令**落地，后端字段保留仅内部用）；「还想问」→ 输入框上方可点 chips（「不知道问什么？可以试试：」，点击回填，最多 3 条）。

**九哥文案裁定存档（2026-09-13，批 2）**：徽章「规则→系统核查」「补盲→待核实」（meta 统计行同步）；「AI 观察」不动；三维度 chip「有没有漏写/有没有自相矛盾/对您的影响」（等待页副标题逐字呼应）；评分卡收起态「仅供参考，点开看明细」；图例 caption「结论怎么看：通过＝这条查了、写得没问题；需关注＝建议重点看；未找到＝合同里没找到这条；不适用＝这类合同不涉及」。红线：四态档位名与「需人工确认」声明零改动。

## 四、调用结构

- 短合同（≤ MAX_CONTRACT_CHARS=6000 字）：单次合并调用（全文+条款目录+规则打标）。
- 长合同：map（`build_review_chunks` 条款对齐切块，≤QUALITY_MAX_SEGMENTS 块，零重试、单块失败跳过）→ 成功块 ≥2 时一次**一致性轮**（跨块矛盾；**不含合同全文**，素材=已过 quote 校验且再洗一遍禁语的观察摘要——伪造原文没有原文可抄）。
- 模型：purpose="review" 研判档；超时 QUALITY_TIMEOUT_SECONDS 回落 LLM_TIMEOUT_SECONDS。

## 五、清洗链（注入对抗核心，顺序固定）

```
fence 剥壳 → json.loads → dimension 白名单（三选一，违者丢条）
→ 限长 title 60 / comment 300 / quote 300
→ clause_id 白名单（编造编号归一 None，不丢条目）
→ quote 全文校验（blind_spot.quote_supported；不过 → 丢条 + dropped_count++）
→ 双禁语表清洗（scorecard_forbidden + llm_ask.BANNED_ECHO）
   清洗打空或残留【已过滤】碎片 → 丢条
→ 去重（同 quote 或同 dimension+title 保留先到）
→ 封顶 12 条、每维度 ≤6
→ needs_confirm=True 代码强制（该字段根本不在模型输出 schema 里）
```

**注入对抗验收**（tests/test_quality.py 埋点六用例）：
① 伪造 quote → 硬丢弃；② 真实文本+禁语 → 清洗后丢条；③ 改档位指令 → 结构性不可能
（深度相等证明）；④ 编造 clause_id → 归一 None；⑤ 刷量 → 封顶；⑥ needs_confirm=false 无效。
已知边界：若注入文本原样携带 quote，那段文字本身「在正文中」会绕过 quote 闸——
此时由禁语二道闸拦截（用例 docstring 已注明）。

## 六、失败矩阵（全部软降级，绝不阻断规则引擎）

| reason | 触发 | 前端 |
|---|---|---|
| disabled | QUALITY_ENABLED=false | 整卡隐藏 |
| no_llm_key | 三家 Key 全无 | 整卡隐藏 |
| llm_error | 调用异常（exc 只进日志） | 整卡隐藏 |
| parse_failed | 主调用/一致性轮两轮均结构错误；长合同 map 全败 | 整卡隐藏 |
| budget_exceeded | 首次调用前预算即尽 | 整卡隐藏 |
| error | 上游解析失败短路 | 整卡隐藏 |

不可用整卡静默隐藏（区别于评分卡的灰字注脚）：质量层是增量信息，缺席不构成疑问。
部分成功（map 中途预算尽但已有合法观察）保留产出 + `coverage.limited=true` 明示。

## 七、预算账目（llm_budget.DEFAULT_BUDGET=16）

预审 ≤2 + 评分/补盲 ≤6 + 质量层 ≤6（长合同 map ≤4 + 一致性 1 + 重试 1）= 最坏 14 ≤ 16。
quality 排预算末位（链上最后一层，先到先得）：预算紧张时最先 budget_exceeded，
规则引擎与既有参考层优先级不被稀释。

## 八、配置与环境变量

| env | 默认 | 说明 |
|---|---|---|
| QUALITY_ENABLED | true | 一键回滚开关：false 即回原状，规则引擎零感知 |
| QUALITY_TIMEOUT_SECONDS | 回落 LLM_TIMEOUT_SECONDS(180) | 后台线程跑，不押同步请求 |
| QUALITY_MAX_SEGMENTS | 回落 LLM_REVIEW_MAX_SEGMENTS(4) | 只砍质量层延迟的独立旋钮 |
| LLM_BUDGET_PER_REVIEW | 16 | 单次审查全链 LLM 调用上限 |

## 九、数据流与 UI

- store：row 新增 `quality` 键（单行 JSON，旧记录无键 → API `quality=None`）；
- schema：`ReviewSummary.quality: Optional[QualityInfo]`（observations/dimension/title/quote/clause_id/comment/needs_confirm/disclaimer/dropped_count/coverage）；
- pipeline：第 4 节点 quality（parse→checklist→model_review→quality→END）；进度段位新增 `analyzing`（第 4 段「AI 观察」；开关关闭时不发该段，等待页自然三段——开启但降级（如 no_llm_key）时该段仍会短暂点亮后完成）；
- 前端：#item-list 之后 #quality-panel 虚线容器（1.5px dashed var(--color-divider) + var(--color-aside-bg) 灰底，零新 token）；描边徽章「AI 观察」；三维度 chip 同款中性灰仅文字区分（零新档位色）；模型态字段一律 textContent 零 innerHTML 拼接；质量条目不可点击进详情/追问。

## 十、明确不做（批 1 范围外）

- ask 四段式重排（批 2）；结果页 IA 重排（批 2）；移动端（批 3）；
- docx 报告塞质量层内容（路线图明令：质量层只在 Web 展示层）；
- 质量层条目参与评分/档位/追问入口。
