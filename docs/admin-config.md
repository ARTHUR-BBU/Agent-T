# 管理员事前配置说明

本文说明上线前管理员要配什么、配在哪里。  
**铁律：检查单（规则引擎）负责打标「通过 / 需关注 / 未找到 / 本类不适用」；LLM 只做人话解释与翻译，从不盖章、从不改写清单状态。**

---

## 一、事前必配（一期）

### 1. 品类开关（采购 / NDA）

| 做什么 | 仓库里的位置 |
|--------|----------------|
| 启用哪些合同品类 | `config/checklist_*.yaml` 的 `category` / `label` |
| 上传时选品类 | API `POST /api/upload` 的 `category`（`procurement` \| `nda`） |
| 品类列表接口 | `app/services/checklist.py` → `list_categories()`（扫描 `config/checklist_*.yaml`） |

当前已有：

- `config/checklist_procurement.yaml` → 采购合同  
- `config/checklist_nda.yaml` → 保密协议  

新增品类：复制一份 YAML、改 `category`/`label`/条目即可，无需改 UI 大框架。

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
- **LLM 不参与打标**；「问清楚一点」只解释已标「需关注」的条目。

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
| NDA 样例 | `fixtures/nda_public_template.txt` |
| 断言 | `tests/test_procurement_gold.py`、`test_procurement_checklist.py`、`test_procurement_paraphrase.py`、`test_procurement_adversarial.py` |

采购金标期望：下列 7 项为「需关注」，「管辖与争议」可为「通过」——

价款与支付、违约责任、格式条款/明显单方不公平、主体、标的、适用法律、签署与印章。

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

## 二、二期预留（写文档，本期不做页面）

以下能力先占位说明，**本期不实现管理页 / 不接产品开关**：

1. **模板库** — 按品类沉淀可复用合同模板与金标对；配置形态可沿用 `fixtures/` + YAML。  
2. **自主研判开关** — 是否允许模型在「规则未命中」时给出参考意见（仍不得覆盖检查单已打标签）。  
3. **报告模板** — 导出/归档用的审查报告版式（封面、政策摘句、需关注汇总）。

实现时再补配置键与页面；当前以本说明为边界。

---

## 三、快速对照表

| 管理员动作 | 改哪里 |
|------------|--------|
| 开关品类 | `config/checklist_*.yaml` |
| 改检查项 / N/A | 同上 `items` |
| 加强同义词打标 | 同上 `rules` + 必要时 `app/services/checklist.py` |
| 改政策摘句 | 同上 `policies` |
| 加金标/对抗样例 | `fixtures/` + `tests/` |
| 开通追问 | `.env`（见 `.env.example`）与 `app/services/llm_ask.py` |

改完规则后请跑：

```bash
pytest -q
```
