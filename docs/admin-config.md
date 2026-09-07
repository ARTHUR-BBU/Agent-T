# 管理员事前配置说明

本文说明上线前管理员要配什么、配在哪里。

## 路径怎么理解（先看这两层）

**默认路径（一期现在就这样）**  
检查单（规则引擎）打标「通过 / 需关注 / 未找到 / 本类不适用」。LLM 只解释已标「需关注」的条款：翻译 + 参谋，**不改状态、不盖章**。

**补盲候选（可开关，默认开）**  
规则未命中时，模型可以提出「发现候选」供人确认；**不得自动改写**检查单已经打好的标签。  
不是「模型永远不许参与发现」——而是发现只能当候选，最终标签仍由人/检查单确认。

---

## 一、事前必配（一期）

### 1. 品类开关（采购 / NDA / 租赁）

| 做什么 | 仓库里的位置 |
|--------|----------------|
| 启用哪些合同品类 | `config/checklist_*.yaml` 的 `category` / `label` |
| 上传时选品类 | API `POST /api/upload` 的 `category`（`procurement` \| `nda` \| `lease`） |
| 品类列表接口 | `app/services/checklist.py` → `list_categories()`（扫描 `config/checklist_*.yaml`） |

当前已有：

- `config/checklist_procurement.yaml` → 采购合同  
- `config/checklist_nda.yaml` → 保密协议（NDA）  
- `config/checklist_lease.yaml` → 租赁合同（承租方视角，法务依据见 `docs/lease-category-legal-opinion.md`）  

新增品类：复制一份 YAML、改 `category`/`label`/条目即可，无需改 UI 大框架。

> ⚠️ **`pass_fulltext_fallback` 使用守卫**（肉饼备案，2026-09-06）：该开关（unless
> 窗口未放行时改查 pass 词表全文）**仅限主体信息这类「完备型检查项」**——其 pass
> 词表必须是「全文出现即意味着信息确实完整」的强 token（法定代表人/统一社会信用
> 代码/住所）。严禁加到 pass 词表含弱信号或可否定 token 的检查项上（典型反例：
> lessor_title 的 pass 词「产权人」会被「未经产权人同意」子串命中，加 flag 等于
> 重开洗白通道）。启用前须逐 token 确认否定上下文不成立。

### 2. 检查单条目 + 哪些可 N/A

| 做什么 | 仓库里的位置 |
|--------|----------------|
| 12 条检查项定义 | 各 `config/checklist_*.yaml` 的 `items:` |
| 本类不适用 | 条目上设 `na: true` + `na_reason`（例：NDA 的 `payment`） |
| 缺项升级为「需关注」 | 条目上设 `missing_as: 需关注`（例：采购的 `governing_law`） |
| 执行引擎 | `app/services/checklist.py` → `run_checklist()`（纯规则，不调 LLM） |

状态只能是引擎给出的四种之一；页面只展示，不二次判定。

### 3. 打标规则（关键词 / 同义词）——只有检查单打标

| 做什么 | 仓库里的位置 |
|--------|----------------|
| 同义/近义组 | YAML 里 `rules.need_attention` / `pass` 的 `any_of`、`unless` |
| 匹配逻辑 | `app/services/checklist.py`（`any_of` / `all_of` / `unless` / `none_of`） |
| 对抗样例回归 | `fixtures/procurement_*.txt` + `tests/test_procurement_*.py` |

要点：

- 优先写同义组，避免单点关键词一改就挂。  
- **默认路径下 LLM 不参与打标**；「问清楚一点」只解释已标「需关注」的条目。

### 4. 政策摘句（给人话依据，不当第二套清单）

| 做什么 | 仓库里的位置 |
|--------|----------------|
| 政策短句 | 各 YAML 顶部/尾部 `policies:` 列表 |
| 展示 | 审查结果里带回，供追问/界面参考 |

政策是「为什么这样标」的依据文案，**不是**另一套检查条目，不要把政策写成可独立打标的规则。

### 5. 金标样例

| 做什么 | 仓库里的位置 |
|--------|----------------|
| 采购金标（原金） | `fixtures/procurement_sample.txt` |
| 同风险换说法 | `fixtures/procurement_paraphrase.txt` |
| 对抗换说法 | `fixtures/procurement_adversarial_2.txt`、`_3.txt` |
| NDA 公开范本（多数可通过） | `fixtures/nda_public_template.txt` |
| NDA 风险金标 | `fixtures/nda_gold_risks.txt` |
| NDA 对抗换说法 | `fixtures/nda_adversarial.txt` |
| 断言 | `tests/test_procurement_gold.py`、`test_procurement_checklist.py`、`test_procurement_paraphrase.py`、`test_procurement_adversarial.py`、`test_nda_gold.py` |

采购金标期望：下列 7 项为「需关注」，「管辖与争议」可为「通过」——

价款与支付、违约责任、格式条款/明显单方不公平、主体、标的、适用法律、签署与印章。

NDA 12 项（`config/checklist_nda.yaml`，品类名「保密协议（NDA）」）：主体（谁跟谁签）、保密范围与义务（什么算秘密、要怎么守）、价款与支付（**始终本类不适用**）、协议期限（这份协议管多久）、保密存续期（秘密要守到哪一天）、违约责任（违约怎么赔）、格式条款 / 单方不公平（有没有一边倒）、知识产权归属（成果/秘密相关权利归谁）、管辖与争议（吵起来找谁管）、解除与终止（怎么收场）、适用法律（按哪国/哪地法律）、签署与印章（谁有权签字、章齐不齐）。

NDA 红线（风险金标 / 对抗样例须为「需关注」，不得「通过」）：缺适用法律（仅有管辖不够）、缺保密存续期或「永久」一刀切、知产归接收方、明显单方不公平、缺签署印章。

### 6. 是否开通「问清楚一点」+ 模型 Key（翻译 + 参谋，不盖章）

| 做什么 | 仓库里的位置 |
|--------|----------------|
| 环境变量模板 | `.env.example` → 复制为 `.env` |
| Key / 模型 / Base | `ZHIPU_API_KEY` 或 `GLM_API_KEY`；`GLM_MODEL`；`ZHIPU_API_BASE` |
| 可选回退 | `XAI_API_KEY` / `GROK_API_KEY`（一般不用） |
| 追问实现 | `app/services/llm_ask.py` |
| 无 Key 时 | 界面/接口提示「追问暂未开通」；清单审查照常可跑 |

「问清楚一点」只做：把难懂条款说成人话、必要时翻译。  
**不做**：改写检查单状态、盖「没问题」章、替管理员拍板。

---

## 二、补盲候选与自主研判（可开关）

把「模型参与发现」和「自主研判」放在同一节，避免两套说法：

| 开关概念 | 默认 | 开启后做什么 | 绝不能做什么 |
|----------|------|--------------|--------------|
| **补盲候选** | **开**（`BLIND_SPOT_ENABLED=true`，亦接受 `1`/`yes`） | 对「未找到」条目和**评分卡点名的缺口**条目（M3.5 定向化：模型在评分卡 JSON 每段输出 `gap_item_ids`，系统把点名条目纳入补盲靶点；点名由**模型自报**，系统只负责校验候选引用的真实性），模型可提出「候选需关注」供人确认；**须引用原文**；无引用则跳过并提示「缺少原文依据，已跳过」 | 自动改写检查单已打标签；自动盖「通过/没问题」；无引用仍展示假「需关注」行 |
| **自主研判** | 关 | 在补盲之上，给出更完整的参考意见（仍是候选） | 覆盖检查单状态；当终审盖章 |

环境变量（见 `.env.example`）：

| 变量 | 默认 | 说明 |
|------|------|------|
| `BLIND_SPOT_ENABLED` | `true` | 关（`false`/`0`/`no`）时无补盲候选、界面零「补盲」文案；**评分卡照常出分** |
| `BASIC_AUTH_USERNAME` / `BASIC_AUTH_PASSWORD` | （空） | 公网部署的整站访问控制；两项**必须同时配置**（只配一半会拒绝启动），不配置则关闭。部署指引见 `docs/deploy-server.md` |
| 追问用 LLM Key | （空） | 无 Key 时评分与补盲都跳过（`reason=no_llm_key`），清单规则仍照常 |

实现位置：`app/services/model_review.py`（审查流水线在 `run_checklist` 之后**最多一次批量 LLM 调用**，同时产出评分卡与补盲候选）。候选挂在审查结果的 `blind_candidates`，**只加分不减分**，不改规则条目的 `status`。

---

## 二点五、模型评分卡（M3.5，纯参考层）

规则打标之后，模型对整份规则结果 + 合同给出百分制评分卡：总分 + 一句话总评 + 7 段分段评语。

| 管理员动作 | 改哪里 |
|------------|--------|
| 分段定义 / 权重 | 各 `config/checklist_*.yaml` 的 `scorecard:` 段（权重合计 100，NA 段 weight 0 + `na: true`） |
| 条目归段 | 条目上的 `segment:` 字段（A~G，见 `docs/m3.5-legal-scorecard-opinion.md`） |
| 禁语清单 | `config/scorecard_forbidden.yaml`（A~E 五类，命中重试一次，再命中降级为仅展示分数） |

硬性门禁（代码强制，不依赖模型自觉，`app/services/scorecard.py`）：

1. **有规则结果才出分**；无 Key 明确「评分暂未开通」，规则结果照常
2. 任一「需关注」或「未找到」→ 总分上限 89；B/D 段（核心内容/违约与退出）有「需关注/未找到」→ 上限 74（未找到比需关注更重，不会出现「挂着未找到却比需关注分高」）
3. 段内每个「需关注」扣 ≥ 段权重 40%、每个「未找到」扣 ≥ 60%
4. 总分 = 分段分重算合计，**不采信模型报的总分**
5. 评语未点名所有非「通过」项 → 降级为仅展示分数（评语**为空**的段由代码自动补点名、不算漏点名；模型写了评语才校验点名完整性）
6. 分数是纯参考信息：**不参与、不改变任何规则档位**，界面灰底展示并常显免责句

专业依据与 cap 阈值 rationale：`docs/m3.5-legal-scorecard-opinion.md`（法务老钱意见书）。

另两项二期占位（本期不做管理页）：

1. **模板库** — 按品类沉淀可复用合同模板与金标对；配置形态可沿用 `fixtures/` + YAML。  
2. **报告模板** — 导出/归档用的审查报告版式（封面、政策摘句、需关注汇总）。

---

## 三、快速对照表

| 管理员动作 | 改哪里 |
|------------|--------|
| 开关品类 | `config/checklist_*.yaml` |
| 改检查项 / N/A | 同上 `items` |
| 加强同义词打标 | 同上 `rules` + 必要时 `app/services/checklist.py` |
| 改政策摘句 | 同上 `policies` |
| 改评分卡分段/权重/归段 | 同上 `scorecard:` 段与 `items[].segment`（M3.5） |
| 改评分禁语 | `config/scorecard_forbidden.yaml`（M3.5） |
| 加金标/对抗样例 | `fixtures/` + `tests/` |
| 开通追问/评分 | `.env`（见 `.env.example`）与 `app/services/llm_ask.py` |
| 开关补盲 | `.env` → `BLIND_SPOT_ENABLED`（默认 true；候选仅，不盖章；评分不受影响） |

改完规则后请跑（pytest 是 dev 依赖，不在 requirements.txt 里）：

```bash
pip install pytest
pytest -q
```
