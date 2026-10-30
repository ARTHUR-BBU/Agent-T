# B7-2 换装执行日志（2026-10-30，部署单 v7+§八关卡）

> 授权链：老板 2026-10-30 三项拍板（合并 #158 / 验收预算方案 A ¥3 /
> 授权换装）。五题付费考试不在本次授权，另行请示。
> **冻结 SHA：`571d179d7776b2fef08cab37ccf98c8b8195a5a2`**（#158 合并后
> main HEAD；源码归档/镜像标签/版本比对三方同一）。

## 执行记录（逐关卡）

| 关卡 | 结果 | 证据 |
|---|---|---|
| 归档+上传（git archive 冻结 SHA） | ✅ | /opt/agent-t-deploy.tar.gz |
| 宿主卷 /opt/agent-t-data 建立并 chown 1000:1000 | ✅ | ls 属主 1000 |
| 预备份（在线 backup API） | ✅ | backup-pre-deploy.db（192KB） |
| 构建 `agent-t:571d179`（-f Dockerfile.server） | ✅ | image ID 2c0e9234fa0c |
| 隔离入口（iptables DOCKER-USER REJECT 8080） | ✅ | 规则在链首；**外部实测**本地 curl → 000（命中实际流量） |
| 在线排空核查（JSON 逐行，fail-closed） | ✅ | `drained`（无 processing/malformed） |
| WAL/SHM 在线记录 | ✅ | **均 ABSENT**（已 checkpoint，迁移按记录仅拷主库） |
| 停机（stop -t 60）+ 改名保留 | ✅ | `agent-t-old-20261030`（保留未删） |
| 终迁移：cp 主库（按记录）→ chown → backup 收敛 | ✅ | integrity ok；**基线 reviews/llm_calls = 1/14**（与部署前探针一致） |
| 新容器（原名 agent-t，挂卷 + STORE_DB_PATH=/data/agent-t-reviews.db，env 继承） | ✅ | Up，Uvicorn startup complete |
| 数据接续 | ✅ | 应用内 reviews/llm_calls = 1/14；revision_drafts/revision_budget 表在位 |
| 安全门（/health 200；/、/api/categories、/docs 401） | ✅ | 服务器本地视角 |
| 版本比对 | ✅ | 容器镜像 = `agent-t:571d179` |
| 新路由探针（handler 级） | ✅ | select/impact → 404 `{"detail":"review_not_found"}`（带认证合法请求体；非 FastAPI 默认 Not Found） |
| 解封+外部恢复 | ✅ | 删规则后本地 /health 200 |
| 业务流-上传审查 | ✅ | review_id `96448be46daf` → done（12 项，7 需关注，scorecard 在） |
| 业务流-导出报告 | ✅ | /api/review/{id}/report → 39KB，**PK 魔数 ✓** |
| 业务流-追问 | ✅ | /api/ask → 实质回答（3132 字，含 evidence/quote_verified，error=None） |
| 验收圈账（严格口径） | ✅ | 6 次调用（precheck/model_review/quality/category_suggest/objection/ask 各 1），**unknown_rows=0**；峰值估算 **¥0.10** ≪ ¥3 控制值 |

## 过程实录（如实）

- stop 退出码 137（60s 后 SIGKILL）——因排空在先且 WAL 不在，数据无影响；
  已记录
- 新路由探针首打 401（Basic Auth 挡在 handler 前）→ 容器内带认证重打；
  再 422（请求体缺字段）→ 补合法请求体后得 handler 级 404 detail
- ask 中文 body 经 git-bash 转码损坏 → body 落文件 `--data-binary` 解决
  （客户端问题，非服务端）
- 导出路由首次打错（/export → Not Found）→ 改用真实路由 /report

## 现场状态

- 生产：`agent-t:571d179`（挂宿主卷 /opt/agent-t-data，STORE_DB_PATH 指向
  /data），外部 /health 200
- 保留物：`agent-t-old-20261030` 旧容器（停用未删，原件在内）、
  /opt/agent-t-data/{backup-pre-deploy.db, mig.db}、服务器
  /tmp/rehearsal-b72 演练目录——**留待老板验收签认后清理**
- 回滚预案（如需）：§四——rm 新容器 → 旧镜像 95555ed+同一 env/卷重启，
  部署后新写入保留

## 复核补证（2026-10-30，codex #159 复批后）

1. **停机 137 的数据无影响证据链（闭合）**：
   - 在线排空确认无 processing、连接已关（WAL/SHM 均 ABSENT）
   - 停机后拷贝的原库（mig.db）与**停机前在线备份**（backup-pre-deploy.db）
     做 reviews+llm_calls **全行内容哈希对比：完全一致**
     （`f805328b300c7348`，1/14）——强杀发生在静止且无未检查点事务的库上
   - **核对范围声明**：哈希覆盖两表全部行全部列；原件（mig.db、
     backup-pre-deploy.db、旧容器）继续保留
2. **¥0.10 口径勘正**：¥0.10 是**账本 usage 峰值牌价估算，非供应商实扣**
3. **precheck 配置差异说明（部署单费用表勘正）**：`PRECHECK_ENABLED`
   代码默认值=**true（开）**（app/services/precheck.py:67），生产未设
   env → 默认开 → 上传真实触发 1 次 precheck（实测在账）。v3 费用表
   「默认关不计」有误，已在部署单 v8 修正（上界 283k → 峰值 ≈¥2.26，
   方案 A ¥3 控制值仍覆盖）
4. **五题考试版本纪律（入待办）**：使用与生产一致的冻结 SHA `571d179`
   checkout + `--server-db /data/agent-t-reviews.db`，方能通过既定版本
   比对

## 后续待办

1. 老板验收签认本执行日志 → 清理保留物
2. 五题付费考试（费用单已备，另行授权；实跑命令带
   `--server-db /data/agent-t-reviews.db`）
3. 老钱逐例语义验收 → B7-2 收官 → B7-3
