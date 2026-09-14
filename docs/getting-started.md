# 安装与配置

[← 返回产品介绍](../README.md)

本指南面向部署与开发。首次体验建议使用仓库自带样例。

## 本地启动

需要 Git 和 Python 3.11+。服务器镜像使用 Python 3.12；希望对齐部署环境时，建议使用该版本。

```bash
git clone https://github.com/ARTHUR-BBU/Agent-T.git
cd Agent-T
```

### Windows PowerShell

直接调用虚拟环境中的 Python，无需修改脚本执行策略：

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --env-file .env
```

### macOS / Linux

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --env-file .env
```

打开 [本地页面](http://127.0.0.1:8000)。必须带 `--env-file .env` 才会加载此文件中的配置；修改配置后重启服务。

`requirements.txt` 用于轻量本地体验；Linux 部署与 CI 使用固定版本的 `requirements-lock.txt`。lock 包含 `uvloop` 等平台相关依赖，不建议直接用于 Windows。

## 先跑一份样例

1. 上传 [`fixtures/procurement_sample.txt`](../fixtures/procurement_sample.txt)，类型选“采购合同”。
2. 当前样例应出现 7 项“需关注”：主体、标的、价款与支付、违约责任、格式条款、适用法律、签署与印章。
3. 点开“价款与支付”，查看说明与原文摘句。
4. 导出 Word 报告；配置模型后，再点“问清楚一点”体验解释与建议。

无模型 Key 时，规则核查与报告导出仍可使用，追问显示“暂未开通”。

macOS / Linux 可在服务启动后运行样例验证脚本：

```bash
bash scripts/demo_procurement.sh
```

服务地址不同时可设置 `BASE_URL`，例如 `BASE_URL=http://127.0.0.1:8000 bash scripts/demo_procurement.sh`。

## 开启 AI 功能

编辑 `.env`，配置实际可用的模型服务凭据。代码按 **DeepSeek → 智谱 → xAI** 的顺序选择已配置的服务。这表示选择优先级，不代表调用失败后会自动切换供应商。

| 服务 | 凭据 | 模型与地址 |
|---|---|---|
| DeepSeek | `DEEPSEEK_API_KEY` | `DEEPSEEK_MODEL`、`DEEPSEEK_API_BASE` |
| 智谱 | `ZHIPU_API_KEY` 或 `GLM_API_KEY` | `GLM_MODEL`、`ZHIPU_API_BASE` |
| xAI | `XAI_API_KEY` 或 `GROK_API_KEY` | `GROK_MODEL` |

模型名称、接口地址与可用额度请按自己的服务账号填写。`.env.example` 中的预设不代表该账号一定拥有相应权限；尤其要核对智谱的 API 地址是否与所购服务匹配。不要将真实 Key 提交到 Git。

| 常用变量 | 当前默认与用途 |
|---|---|
| `PRECHECK_ENABLED` | `true`：AI 预审分类；不可用时可跳过并继续规则审查 |
| `BLIND_SPOT_ENABLED` | `true`：允许生成待人工确认的候选风险；关闭不会关闭评分或质量观察 |
| `QUALITY_ENABLED` | `true`：开启 AI 完整性、一致性、影响观察 |
| `LLM_TIMEOUT_SECONDS` | `180`：评分/追问等调用的超时配置 |
| `PRECHECK_TIMEOUT_SECONDS` | `30`：预审独立超时 |
| `QUALITY_TIMEOUT_SECONDS` | 未设置时回落到 `LLM_TIMEOUT_SECONDS` |
| `XAI_TIMEOUT_SECONDS` | `60`：xAI 默认调用超时，部分场景可单独覆盖 |
| `LLM_BUDGET_PER_REVIEW` | `16`：每次上传审查的模型调用次数预算；不包含手动追问 |
| `LLM_REVIEW_MAX_SEGMENTS` | `4`：长合同评分/补盲阅读块数上限 |
| `QUALITY_MAX_SEGMENTS` | 未设置时回落到 `LLM_REVIEW_MAX_SEGMENTS` |
| `RATE_LIMIT_UPLOAD_PER_MINUTE` | `10`：每 IP 每分钟上传次数 |
| `RATE_LIMIT_ASK_PER_MINUTE` | `20`：每 IP 每分钟追问次数 |
| `STORE_DB_PATH` | SQLite 文件路径，默认系统临时目录 |
| `STORE_TTL_HOURS` | `24`：记录有效期；`0` 表示永久保存 |

预审与正式审查可使用不同模型，通过 `DEEPSEEK_MODEL_PRECHECK` / `DEEPSEEK_MODEL_REVIEW` 配置；智谱和 xAI 分别使用 `GLM_MODEL_*`、`GROK_MODEL_*`。更多含义见 [管理员说明](admin-config.md)。

## 文件解析与数据范围

- `.txt` / `.md`：可直接体验。
- `.docx`：基础依赖已包含 python-docx。
- 文字版 PDF：在当前虚拟环境中安装 `pypdf`；Windows 使用 `.\.venv\Scripts\python.exe -m pip install pypdf`，macOS / Linux 使用 `python -m pip install pypdf`。
- 扫描件及旧版 `.doc`：不能保证基础环境可解析，建议先转换。Docling 是可选的较重解析路径，不是快速体验的必要依赖。

合同文本保存在部署服务器的 SQLite 中。配置模型时，相关文本会发送到所选供应商或自定义 API 网关。Word 报告含逐条结果、原文摘句、参考评分和补盲候选，当前不含质量观察与追问回答。

默认 24 小时后记录不能再访问。当前物理清理由新建记录及访问过期记录时触发，不是独立定时删除任务，也不应等同于磁盘安全擦除。请按自己的数据保存要求配置和部署。

## 测试

在已激活的环境中运行下列命令；Windows 未激活时，将 `python` 替换为 `.\.venv\Scripts\python.exe`。

```bash
python -m pip install pytest
python -m pytest -q
```

需要运行浏览器 E2E 时，再安装 Playwright 与 Chromium：

```bash
python -m pip install playwright
python -m playwright install chromium
python -m pytest tests/test_frontend_e2e.py -q
```

未安装 Playwright 包时，对应测试模块跳过；已装包但未安装浏览器时，浏览器测试会失败。真实模型专项需要单独配置凭据；一般回归建议使用无真实 Key 的测试环境，避免意外产生模型调用费用。

## API 与代码入口

| 接口 | 用途 |
|---|---|
| `GET /api/categories` | 合同品类及支持的立场 |
| `POST /api/upload` | 上传文件；表单含 `file`、`category`、可选 `stance` / `force` |
| `GET /api/review/{id}` | 查询处理状态与结果 |
| `POST /api/ask` | `{ review_id, item_id, question }`，仅对规则标记“需关注”的条目追问 |
| `GET /api/review/{id}/report` | 导出已完成审查的 Word 报告 |
| `GET /health` | 服务探活 |

上传可能返回品类确认要求；正式审查在后台运行，取得 `review_id` 后轮询结果。启动后可在 [本地 API 文档](http://127.0.0.1:8000/docs) 查看请求结构。

```text
app/api/       API 与数据结构
app/graph/     审查流程编排
app/services/  解析、规则、AI 分析、存储与报告
app/prompts/   提示词与共享约束
app/static/    Web 界面
config/        采购、租赁、NDA 的规则配置
fixtures/      示例与回归语料
tests/         自动化测试
```

## 部署到服务器

参见 [服务器部署指南](deploy-server.md)。公网环境应同时配置 `BASIC_AUTH_USERNAME` 与 `BASIC_AUTH_PASSWORD`，并在接入真实合同前启用 HTTPS。当前并发与限频实现基于单进程设计。

部署文档中的历史环境说明可能滞后；当前持久化实现是 SQLite，重启进程与重建未挂载数据卷的容器，数据保留行为不同。需要持续保存记录时，应明确数据库路径、数据卷和保留期限。
