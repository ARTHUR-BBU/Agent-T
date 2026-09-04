# Agent-T · 合同审查 Agent（MVP）

上传一份合同（PDF / Word / txt）→ 按可配置 12 项清单做规则优先审查 → 仅对「需关注」项用 Grok 追问。

**范围（本 MVP）**：单文档；无企微；LangGraph **库**编排（非 LangGraph Platform）。

## 快速开始

```bash
cd Agent-T
python3.11 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 可选：Grok 追问
export XAI_API_KEY=xai-...   # 或 GROK_API_KEY

# 启动
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

浏览器打开 <http://localhost:8000>。

### 用采购样例演示

1. 上传 `fixtures/procurement_sample.txt`，类型选「采购合同」
2. 应看到至少 7 条「需关注」（价款、违约、格式条款、主体、标的、适用法律、签署）
3. 点开某条「需关注」→「问清楚一点」（需配置 API Key；无 Key 时清单仍可用，追问返回明确提示）

## 环境变量

| 变量 | 说明 |
|------|------|
| `XAI_API_KEY` 或 `GROK_API_KEY` | xAI Grok HTTP API；不配则清单审查可用，`/api/ask` 返回清晰错误 |
| `GROK_MODEL` | 可选，默认 `grok-2-latest` |

> 预留：`app/services/grok.py` 中的 `GrokAuthProvider` 接口，供未来用户 OAuth 登录后按用户取 Key。

## API

- `POST /api/upload` — `multipart/form-data`：`file` + 可选 `category`（`procurement` \| `nda`）
- `GET /api/review/{id}` — 审查结果与 12 项状态
- `POST /api/ask` — `{ review_id, item_id, question }`，仅「需关注」
- `GET /api/categories` — 可用清单类别
- `GET /health`

状态枚举：`通过` / `需关注` / `未找到` / `本类不适用`（UI 文案：已通过 / 需关注 / 未找到 / 本类不适用）。

## 清单与规则

- 配置：`config/checklist_procurement.yaml`、`config/checklist_nda.yaml`
- NDA 下「价款与支付」为 `本类不适用`
- 规则优先（关键词 / 正则）；Grok **不**参与打标，只做需关注追问，且必须引用原文，不得盖章「没问题」

政策要点（注入 Grok 上下文，非独立清单项）：

- 先验收后付款；小额货 90%+10% 质保金
- 质保≥12 个月；签收≠验收完
- 办公设备原则上不找小规模纳税人
- 无合规发票可拒付

## 管线（LangGraph 库）

`parse`（Docling 可选 / 文本直读）→ `checklist`（YAML 规则）→ `grok_ready`（追问走 `/api/ask`）

PDF/Word：若已安装 `docling`（或 `python-docx` / `pypdf`）则用之；**始终**支持 `.txt`，保证无重依赖也能演示。

## 测试

```bash
pytest -q
```

采购金标样例须对下列 7 项打出「需关注」：价款与支付、违约责任、格式条款/明显单方不公平、主体、标的、适用法律、签署与印章；「管辖与争议」可通过。

## 项目结构

```
app/
  main.py              # FastAPI
  api/routes.py        # upload / review / ask
  services/extract.py  # 文本抽取
  services/checklist.py
  services/grok.py     # xAI + OAuth stub
  graph/pipeline.py    # LangGraph
  static/              # 三屏前端
config/                # YAML 清单
fixtures/              # 采购样例 + NDA 范本
tests/
```
