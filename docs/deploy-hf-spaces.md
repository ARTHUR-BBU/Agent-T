# 部署指南：Hugging Face Spaces（免费）

目标：让合作伙伴通过一个网址直接访问和测试 Agent-T，零服务器成本。

## 为什么选 HF Spaces

- 免费 CPU：2 vCPU / 16GB 内存——docling 解析大 PDF 不怕 OOM
- 不绑卡、不实名，注册即用
- 代价（提前知晓）：
  - `*.hf.space` 域名在国内被墙，**访问需挂梯子（全局模式）**
  - 48 小时无访问会休眠，再访问要等 1-2 分钟冷启动（Settings 里可手动重启）
  - Space 代码公开（与 GitHub 同等可见性），所以**密钥只能走 Secrets，绝不能写进代码**

## 安全前提（已内置，无需改代码）

- 整站 HTTP Basic Auth：配置 `BASIC_AUTH_USERNAME` / `BASIC_AUTH_PASSWORD` 后自动启用
  （`app/auth.py`，除 `/health` 外全站设防；不配置则关闭）
- `.dockerignore` 已排除 `.env`——本机真密钥不会进镜像
- API Key 走 Space Secrets 注入，不落仓库

## 部署步骤（约 30 分钟，其中等待构建 ~15 分钟）

### 1. 注册并创建 Space

1. 挂梯子，注册/登录 https://huggingface.co
2. 右上角头像 → New Space
3. Space name：`agent-t`（可自定）；SDK 选 **Docker** → Blank 模板
   （Docker 模板默认 Visiblity 为 Public——免费额度只有 Public 可用，靠 Basic Auth 保内容安全）
4. 创建后进入 Space 页面，记下地址：`https://huggingface.co/spaces/<你的用户名>/agent-t`

### 2. 推代码（git 方式，推荐）

```bash
# 在仓库目录外 clone 空的 Space 仓库（会提示登录，用户名是 HF 用户名，密码用 Access Token）
git clone https://huggingface.co/spaces/<你的用户名>/agent-t hf-space
# 把项目文件拷进去（注意：不要拷 .env！.dockerignore 必须带上——
# 它防止 fixtures 合同样本等随镜像进公开 Space，肉饼审计 P2-1/P2-2）
cd Agent-T
cp -r app config requirements.txt requirements-hf.txt Dockerfile .dockerignore .env.example ../hf-space/
# docs/fixtures/tests/scripts 不拷，Space 运行不需要
cd ../hf-space
git add . && git commit -m "deploy: Agent-T on HF Spaces"
git push
```

没有 Access Token 就去 HF：Settings → Access Tokens → 创建一个 **write** 权限 token。

### 3. 配置 Secrets（关键一步）

Space 页面 → **Settings** → **Variables and secrets**，添加三个 Secret：

| Name | Value | 说明 |
|------|-------|------|
| `ZHIPU_API_KEY` | 你的智谱 Key | 追问/评分/补盲生效；不配则只有清单审查 |
| `BASIC_AUTH_USERNAME` | 自定（如 `partner`） | 告诉合作伙伴的账号 |
| `BASIC_AUTH_PASSWORD` | 自定强密码 | 同上；别和 Key 复用 |

改完 Secrets 会自动重启 Space。

### 4. 等构建 → 验证

1. Space 页面看 **Building** 日志（首次约 10-20 分钟，docling/torch 依赖大，属正常）
2. 变成 **Running** 后访问 `https://<你的用户名>-agent-t.hf.space`
3. 浏览器应弹出账号密码框 → 输入第 3 步配置的账密 → 看到上传页
4. 传一份合同完整跑一遍：审查结果 → 导出报告 → 追问（有 Key 时）

### 5. 交给合作伙伴

发给他们三样东西：

```
网址：https://<你的用户名>-agent-t.hf.space
账号：<BASIC_AUTH_USERNAME>
密码：<BASIC_AUTH_PASSWORD>
（提示：国内访问需开梯子，全局模式；首次打开若较慢是在冷启动，等 1-2 分钟）
```

## 日常更新代码

```bash
cd hf-space
# 覆盖 app/ 等目录后
git add . && git commit -m "update" && git push
```

## 常见问题

| 现象 | 原因与处理 |
|------|-----------|
| 打开要等 1-2 分钟 | 48h 无访问休眠后的冷启动，属正常；频繁演示可在 Settings → Restart 手动预热 |
| 弹密码框输错一直弹 | 清掉浏览器该站点的认证缓存，或换无痕窗口重试 |
| 追问/评分显示「暂未开通」 | `ZHIPU_API_KEY` Secret 没配或配错，检查 Secrets 拼写 |
| 构建失败 | 看 Building 日志；最常见是依赖版本冲突，把日志发给开发狗 |
| 想换账密 | 改 Secrets 即可，自动重启生效 |

## 备选方案（如 HF 体验不满足）

- **ClawCloud Run**（$5/月永久额度，GitHub 账号>180 天可领）：真常驻不休眠，东京/新加坡节点国内直连快，Docker 镜像部署，本项目 Dockerfile 可直接复用
- **阿里云/腾讯云轻量服务器试用**（1-3 个月，需国内实名）：大陆访问体验最优，IP:端口直连免备案
