# Agent-T · 合同审查 Agent（MVP）

**是做什么的**  
上传一份合同 → 按 12 条清单挑问题 → 对「需关注」点「问清楚一点」用人话解释（不盖「没问题」章）。

**范围**：一次只审一份；不用企微/钉钉。底层用现成脚手架（FastAPI、LangGraph **库**），不自研调度平台。

## 最短跑起来

1. 装 Python 3.11+，进入本仓库目录  
2. 创建并激活虚拟环境，装依赖：

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

3. 复制环境变量模板（**没有 Key 也能先看清单**；追问会显示「追问暂未开通」）：

```bash
cp .env.example .env
# 若要演示「问清楚一点」，编辑 .env 填入 ZHIPU_API_KEY
# Coding 套餐保持 ZHIPU_API_BASE=https://open.bigmodel.cn/api/coding/paas/v4
```

4. 启动（**必须带 `--env-file`**，否则 `.env` 不会生效、Key 不被读取）：

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --env-file .env
```

5. 浏览器打开 <http://localhost:8000>

## 三分钟演示（页面）

1. 上传 `fixtures/procurement_sample.txt`，类型选「采购合同」  
2. 应看到约 **7 条「需关注」**（价款与支付、违约责任、格式条款、主体、标的、适用法律、签署与印章）  
3. 点「价款与支付」看原文黄底命中词  
4. 再点「问清楚一点」（有 Key 才有智能解释；无 Key 显示「追问暂未开通」）

## 一键演示脚本（命令行）

服务先按上面启动着，另开终端：

```bash
source .venv/bin/activate
bash scripts/demo_procurement.sh
```

脚本会：上传采购样例 → 断言 7 条「需关注」→ 若已配置 Key 则追问「价款与支付」，否则打印「追问暂未开通」。

可选：`BASE_URL=http://127.0.0.1:8000 bash scripts/demo_procurement.sh`

## 术语一句

- **清单** = 事先定好的必看项  
- **需关注** = 建议你仔细看  
- **智能解释** = 把难懂条款说成人话（不盖章）

## 环境变量（`.env.example`）

| 变量 | 说明 |
|------|------|
| `DEEPSEEK_API_KEY` | **首选**（OpenAI 兼容，速度快）。GLM Coding Plan 条款限定指定工具内使用，web 服务调用会被降权，故线上优先走 DeepSeek |
| `DEEPSEEK_MODEL` | 默认 `deepseek-v4-flash`；追问可配 `deepseek-v4-pro` |
| `DEEPSEEK_API_BASE` | 默认 `https://api.deepseek.com`；兼容其他 OpenAI 协议网关 |
| `ZHIPU_API_KEY` 或 `GLM_API_KEY` | 智谱 Key（备选回退）；不配也能跑清单 |
| `GLM_MODEL` | 默认 `glm-5.2` |
| `ZHIPU_API_BASE` | Coding 套餐默认 `https://open.bigmodel.cn/api/coding/paas/v4`；标准 API 改为 `https://open.bigmodel.cn/api/paas/v4` |
| `XAI_API_KEY` / `GROK_API_KEY` | 可选回退（**一般不用**） |
| `BLIND_SPOT_ENABLED` | 补盲开关，默认 `true`；关则纯规则、界面无「补盲」 |
| `LLM_TIMEOUT_SECONDS` | 单次大模型调用超时，默认 180 |
| `PRECHECK_ENABLED` | LLM 预审（上传时合同分类+支持性判断）开关，默认 `true`；设 `false` 退回纯规则行为 |
| `PRECHECK_TIMEOUT_SECONDS` | 预审独立超时秒数，默认 30 |
| `STORE_DB_PATH` / `STORE_TTL_HOURS` | 审查记录 SQLite 路径 / 保留时长（默认 24h，0=永久） |

> 数据流向说明：配置了模型 Key 时，上传合同的**文本内容**会发送至所配大模型（DeepSeek/智谱）用于评分、补盲与追问；不配置 Key 则纯本地规则审查，数据不出服务器。

## 测试

```bash
pip install pytest    # 或 pip install -e ".[dev]"；pytest 不在 requirements.txt 运行时依赖里
pytest -q
```

前端 E2E（`tests/test_frontend_e2e.py`，Playwright 真实浏览器，无 Key 模式）：

```bash
pip install playwright
playwright install chromium
```

未安装 Playwright 时该模块整体 skip，不影响其余用例；只装了包但没跑 `playwright install chromium` 则会在 fixture 处报错（而非 skip），两条安装命令都要执行。

采购金标：上述 7 项须为「需关注」；「管辖与争议」可通过。

## API 速查

- `POST /api/upload` — 文件 + `category`（`procurement` \| `nda` \| `lease`）  
- `GET /api/review/{id}` — 审查结果  
- `GET /api/review/{id}/report` — 导出审查报告（docx，M4）  

## 部署

公网部署（国内云服务器 Docker，含 Basic Auth 访问控制）见 [docs/deploy-server.md](docs/deploy-server.md)；
`BASIC_AUTH_USERNAME` / `BASIC_AUTH_PASSWORD` 同时配置即启用整站认证（不配置则关闭）。
- `POST /api/ask` — `{ review_id, item_id, question }`（仅需关注）  
- `GET /health`

## 设计变量

界面色板/字号见 [`docs/design-tokens.md`](docs/design-tokens.md)（阳仔视觉标准 v0.1）。

## 项目结构

```
app/           # FastAPI + 清单引擎 + 追问 + 三屏静态页
config/        # 采购 / NDA 清单 YAML
fixtures/      # 采购样例、换措辞对抗样例、NDA 模板
scripts/       # demo_procurement.sh
tests/
```
