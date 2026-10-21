# 部署指南：国内云服务器（Docker）

目标：让合作伙伴通过 `http://<服务器IP>:8080` 直接访问和测试 Agent-T。
当前线上实例：阿里云 ECS 试用（2026-09-07 上线）。

> 📌 历史备案：原定 Hugging Face Spaces 方案在部署当天实测被 402 拦截——
> HF 已取消 Docker/Gradio Space 的免费档（需 PRO 订阅 $9/月），免费只剩纯静态
> Space。遂切换阿里云 ECS 路线（法务/合规上 IP:端口直连无需域名备案）。

## 架构

- Ubuntu 22.04 + Docker CE（阿里云 apt 源 + daocloud/1ms registry mirror）
- `Dockerfile.server`：**不含 docling**（torch 系依赖太重，2G 内存扛不住）——
  `app/services/extract.py` 兜底链保证 `.docx`→python-docx、`.pdf`→pypdf 照常解析；
  仅扫描版 PDF（无文本层）会提示改传文本
- 整站 Basic Auth（`BASIC_AUTH_USERNAME/PASSWORD`，半配置启动即拒绝——fail-closed）
- API Key（`ZHIPU_API_KEY`）以容器环境变量注入，不落盘、不进仓库

## 从零部署（新实例 runbook）

```bash
# 0. 本地推公钥，禁用密码登录（Windows 无 sshpass，用 paramiko 或手动）
ssh-copy-id root@<IP>

# 1. 服务器加固：加 4G swap（2G 机器构建保险）
ssh root@<IP> 'fallocate -l 4G /swapfile && chmod 600 /swapfile && \
  mkswap /swapfile && swapon /swapfile && \
  echo "/swapfile none swap sw 0 0" >> /etc/fstab && \
  sed -i "s/^#\?PasswordAuthentication.*/PasswordAuthentication no/" /etc/ssh/sshd_config && \
  systemctl reload sshd'

# 2. 装 Docker（阿里云源）+ registry mirror（daemon.json 写完必须 restart docker！）
#    完整命令见 git history（本文件作者提交记录），坑：mirror 配置不重启不生效，
#    基础镜像会直连 docker.io 超时

# 3. 传代码 + 构建 + 起容器
git archive --format=tar.gz HEAD -o /tmp/agent-t.tar.gz
scp /tmp/agent-t.tar.gz root@<IP>:/opt/ && ssh root@<IP> \
  'mkdir -p /opt/agent-t && tar xzf /opt/agent-t.tar.gz -C /opt/agent-t'
ssh root@<IP> 'cd /opt/agent-t && docker build -f Dockerfile.server -t agent-t:latest .'
ssh root@<IP> "docker run -d --name agent-t --restart unless-stopped -p 8080:8080 \
  -e PORT=8080 \
  -e BASIC_AUTH_USERNAME=<账号> \
  -e BASIC_AUTH_PASSWORD='<强密码>' \
  -e ZHIPU_API_KEY='<智谱Key>' \
  agent-t:latest"
```

> **排障提示**（外部审计批3）：容器用 `--restart unless-stopped`，若启动阶段
> 校验失败（认证变量只配一半 / 限频变量非法 / checklist 配置正则写坏）会
> **无限重启循环**。容器反复重启时先 `docker logs agent-t` 看具体
> ValueError，改正环境变量或配置后 `docker restart agent-t`。

## 日常更新代码

> **⚠️ 换容器前必先备份账本**（B2-3 受控试运行事故教训，2026-10-18）：
> reviews.db 在容器内 /tmp，`docker rm -f` 会连带销毁——先 `docker cp`
> 备份到宿主机再做任何容器操作。

```bash
git archive --format=tar.gz HEAD -o /tmp/agent-t.tar.gz
scp /tmp/agent-t.tar.gz root@<IP>:/opt/
# ① 备份账本（换容器前必做）
ssh root@<IP> 'mkdir -p /opt/backup && docker cp agent-t:/tmp/agent-t-reviews.db /opt/backup/agent-t-reviews-$(date +%Y%m%d%H%M).db'
# ② 构建+换容器
ssh root@<IP> 'cd /opt/agent-t && tar xzf /opt/agent-t.tar.gz && \
  docker build -f Dockerfile.server -t agent-t:latest . && \
  docker rm -f agent-t'
# 然后重跑上面的 docker run（env 不变）
```

### B2-3 建议层配置核对（开启 CATEGORY_SUGGEST_ENABLED 时必做）

**顺序**：先开启开关并完成至少一次受控上传（触发建议层），再执行核验——
核验命令 fail-loud：**零调用或任何失败/降级记录都以退出码 1 报错**，
不存在「零调用也 PASS」的假绿。

```bash
ssh root@<IP> 'docker exec agent-t python3 -c "
import sqlite3, sys
con = sqlite3.connect("/tmp/agent-t-reviews.db")
rows = con.execute("SELECT outcome, error_detail, trace_id, prompt_hash, raw_output FROM llm_calls WHERE node=:n AND trace_id=(SELECT trace_id FROM llm_calls WHERE node=:n AND trace_id IS NOT NULL ORDER BY created_at DESC LIMIT 1)", {"n": "category_suggest"}).fetchall()
if not rows:
    raise SystemExit("FAIL: 未发现 category_suggest 调用；请先完成受控上传")
bad = [r for r in rows if r[0] != "success"]
if bad:
    raise SystemExit(f"FAIL: 建议层存在失败/降级记录：{bad}")
# B2-3 两项升级专项：留痕硬接口完整性——只核最新一次调用（trace_id 限定，
# 升级前历史行三列为 NULL 属预期，不参与核验）
missing = [r[1] for r in rows if not r[2] or not r[3] or not r[4]]
if missing:
    raise SystemExit(f"FAIL: 建议层账本行缺 trace_id/prompt_hash/raw_output：{missing}")
print("PASS:", len(rows), "category_suggest calls (latest trace), trail complete")
"'
# 同时核对容器 env 含 CF_API_TOKEN 与 CF_ACCOUNT_ID（变量名核对即可，勿回显值）；
# 降级名单黑话：no_llm_key=缺 CF_API_TOKEN / no_account_id=缺 CF_ACCOUNT_ID
# （出现即配置缺失，建议层未真正工作）；
# budget_exceeded=预算闸拦截（emit_skip 显式账，非故障——核对额度配置是否合理）
```

## 账本（llm_calls）schema 与回滚纪律（B2-3 两项升级专项起生效）

- 账本表结构**只增不删、向前兼容**：新列（trace_id/raw_output/prompt_hash）由
  启动时幂等迁移自动添加（`PRAGMA table_info` 检缺列再 ALTER，并发首启有锁守卫）。
- **常规回滚 = 回退到同样使用显式列名写入的兼容版本，或前向热修**。
  **禁止**以「删列（DROP COLUMN）」或「恢复旧备份库」作为常规回滚手段——
  删列丢新留痕、恢复旧库丢部署期间新产生的审查与账本。
- 「恢复部署前备份库 / DROP COLUMN」仅保留给**灾难恢复场景**（库损坏/数据污染）；
  与常规回滚是两回事，执行前必须向用户报备。

## 部署验收安全门（每次部署/更新后必跑）

```bash
curl -s -o /dev/null -w "%{http_code}" http://<IP>:8080/health          # 200（探活豁免）
curl -s -o /dev/null -w "%{http_code}" http://<IP>:8080/                # 401
curl -s -o /dev/null -w "%{http_code}" http://<IP>:8080/api/categories  # 401
curl -s -o /dev/null -w "%{http_code}" http://<IP>:8080/docs            # 401
curl -s -o /dev/null -w "%{http_code}" -u <账号>:<密码> http://<IP>:8080/  # 200
# 变体不得借道：//health、/health/、/HEALTH、/healthx → 非 200
```

再加一次完整业务流：上传合同 → 结果 → 导出报告（PK 魔数）→ 追问可用。

## 给合作伙伴的话术模板

```
网址：http://<服务器IP>:8080
账号：<BASIC_AUTH_USERNAME>
密码：<BASIC_AUTH_PASSWORD>
（浏览器会弹出账号密码框；国内直连无需梯子）
```

## 注意事项

- **凭据文件解析必须剥标签+断言 ASCII**（2026-10-22 部署事故教训）：
  cloudflare.txt 等人工凭据文件可能带中文标签（「账户：」「令牌：」）——
  直接整行灌进 env 会把标签混进 API Token，外部调用全部静默降级
  （建议层 llm_error，账本留痕、fail-loud 零调用拦住，未误报上线）。
  规矩：①解析时剥「：/:」前缀标签；②注入前断言值纯 ASCII；
  ③降级话术黑话（no_llm_key/llm_error）出现在受控上传响应=立即查凭据注入
- **实例按量付费/试用有期限**：到期释放会丢公网 IP 和数据；长期用转包年包月或绑 EIP
- 容器内是内存存储：重启容器 = 清空审查记录（隐私上反而是优点）
- 改 Basic Auth 账密 / ZHIPU Key：`docker rm -f agent-t` 后重跑 docker run 换 env
- 别把真实 Key/密码写进任何 git 跟踪的文件
