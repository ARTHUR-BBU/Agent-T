# B7-2 部署单 v3（2026-10-30，按 #158 三轮复批修正）

> 性质：**部署前备料**（换装与付费开考分别待老板授权；¥0.59/¥2 均为
> 估算口径的预算控制值，非已证明的现金绝对上限，实付以供应商账单为准）。
> v3 修正：考试工具库路径对齐、切换序列消除写入空窗、宿主卷权限落实、
> 验收费用给可复算公式。

## 一、版本核实与三方对齐

**生产实态（2026-10-30 只读探针）**：在跑 `agent-t:95555ed`（含 #148），
/health 200；**无宿主挂载**（Mounts 空/Binds null）；库
`/tmp/agent-t-reviews.db`，`journal_mode=wal`；当时数据基线 reviews=1 /
llm_calls=14（**切换时须重采**，非长期基线）；生产 env 开关清单（只读
探针，凭据值未读）：CATEGORY_SUGGEST_ENABLED 开、DEEPSEEK_API_KEY 在位、
无 PRECHECK/QUALITY 显式开关（QUALITY 默认开）。

**三方对齐**：#158 合并后**冻结 main HEAD 全 SHA**（记入执行日志）；
镜像 `agent-t:<该SHA>`（弃 latest——`check_revision` 只收 Git SHA 标签，
已核代码）；考试工具同 SHA checkout 且 `--server-db` 与应用库路径一致
（见 §二.4）；比对用 runner 同款 `server_revision`+`check_revision`。

## 二、数据接续方案 v3（三轮复批修正）

### 2.1 应用与考试工具同库路径（P1-1）

- runner 新增 `--server-db` 参数（**已合并代码+测试**：
  `test_server_db_flows_to_all_fragments` 断言种单/账本/额度/严格圈账/
  节点核对/清理六个片段全部用同一路径）
- **换装后实跑命令必须带 `--server-db /data/agent-t-reviews.db`**；
  贯通验证见 §2.3 步骤 7

### 2.2 权限落实（P1/P2）

- 镜像以 `appuser`（uid 1000）运行（Dockerfile.server 已核）
- 宿主卷目录以 **root 建立后必须 `chown 1000:1000 /opt/agent-t-data`**；
  数据库迁入后同属 1000（`docker cp` 以 root 执行会落 root 属主——
  cp 后补 chown）
- **只读/写入双探针**（真容器真用户执行，通过才继续）：
  ```bash
  # 读探针：appuser 能读迁移库
  docker run --rm -v /opt/agent-t-data:/data agent-t:<SHA> \
    python3 -c "import sqlite3; c=sqlite3.connect('file:/data/agent-t-reviews.db?mode=ro',uri=True); print('rows',c.execute('SELECT COUNT(*) FROM reviews').fetchone()[0])"
  # 写探针：appuser 能建/写库及 WAL/SHM 配套文件
  docker run --rm -v /opt/agent-t-data:/data agent-t:<SHA> \
    python3 -c "import sqlite3; c=sqlite3.connect('/data/.write-probe.db'); c.execute('CREATE TABLE t(x)'); c.execute('INSERT INTO t VALUES (1)'); c.commit(); print('write ok')"
  ```
- `docker cp`/`chown` 等宿主机操作**全部经 ssh 在服务器上执行**（文档
  所示命令均在 ssh 会话内跑，非本地）

### 2.3 切换序列 v5（停机隔离排空 + WAL 显式检测 + 权限前移）

**技术隔离手段**：本序列以 `docker stop` 为新写入的**技术阻断**——
容器停止即服务不可达，不存在「仅通知不技术上挡新上传」的空窗；排空
核查在停机后对静止库做（fail-closed）。按序执行，任一步失败走 §四：

1. **预告**：低峰窗口，伙伴群通知（告知短暂不可用时段）——通知不承担
   隔离职责，隔离由步骤 2 的停机实现
2. **预备份**（旧容器在线，backup API 一致性快照 → 宿主卷；此时仅作
   双保险，非迁移源）+ 宿主卷目录 `mkdir`+`chown 1000:1000` 先行
3. **构建镜像**：`git archive <冻结SHA>` → scp → `-f Dockerfile.server`
   → `agent-t:<SHA>`——构建期间旧容器照常服务
4. **停机排空**：`docker stop -t 60 agent-t`（SIGTERM 给足在途请求收尾；
   停机=新写入技术阻断）。**排空核查（fail-closed，按真实表结构查
   JSON）**：
   ```bash
   docker exec agent-t python3 -c "
   import sqlite3, json, sys
   c = sqlite3.connect('/tmp/agent-t-reviews.db')
   bad = []
   for rid, created, data in c.execute('SELECT id, created_at, data FROM reviews'):
       try:
           d = json.loads(data)
       except ValueError:
           bad.append((rid, 'malformed'))  # 解析不了=无法证实已完成，fail-closed
           continue
       if d.get('status') == 'processing':
           bad.append((rid, 'processing'))
   if bad:
       print('NOT_DRAINED:', bad); sys.exit(1)
   print('drained')"
   ```
   非 0 退出 → **中止换装**：`docker start agent-t` 复活原服务，向老板
   报备，不误报成功。通过才进下一步。
5. **终迁移（WAL 显式检测+权限前移+原件保留）**：
   ```bash
   # ① 容器内显式检测 WAL/SHM 是否存在（ABSENT/EXISTS 二值输出；
   #    exec 失败 rc≠0 即中止——不得把执行失败当文件不存在）
   docker exec agent-t sh -c \
     'if [ -f /tmp/agent-t-reviews.db-wal ]; then echo EXISTS; else echo ABSENT; fi'
   # ② 整体拷出文件集（WAL 存在则必须拷贝成功——cp 失败=中止并保留
   #    旧容器，禁止 2>/dev/null || true 吞错；SHM 按 SQLite 恢复方案
   #    属可丢弃索引：拷出留档，恢复时不参与）
   docker cp agent-t:/tmp/agent-t-reviews.db /opt/agent-t-data/mig.db
   docker cp agent-t:/tmp/agent-t-reviews.db-wal /opt/agent-t-data/mig.db-wal   # EXISTS 时必查 rc
   docker cp agent-t:/tmp/agent-t-reviews.db-shm /opt/agent-t-data/mig.db-shm 2>/dev/null || echo "shm 无/可丢弃（SQLite 恢复语义）"
   # ③ 权限前移（v5 修正：先修属主再让 uid1000 容器读，root cp 落 root
   #    属主，不先修则 backup 必失败）
   chown -R 1000:1000 /opt/agent-t-data
   # ④ 读探针（appuser 真能读文件集，权限错误不得误判为 WAL 不存在）
   docker run --rm -v /opt/agent-t-data:/data agent-t:<SHA> python3 -c "
   import os
   for f in ('mig.db',) + ((('mig.db-wal',) if os.path.exists('/data/mig.db-wal') else ())):
       open('/data/'+f, 'rb').read(1)
   print('read ok')"
   # ⑤ backup API 收敛单文件（appuser 容器执行）+ 完整性断言 + 源/快照
   #    逐项核对（行数一致才算迁移成立）
   docker run --rm -v /opt/agent-t-data:/data agent-t:<SHA> python3 -c "
   import sqlite3, json
   src = sqlite3.connect('/data/mig.db')
   dst = sqlite3.connect('/data/agent-t-reviews.db')
   src.backup(dst); dst.close(); src.close()
   ok = sqlite3.connect('/data/agent-t-reviews.db').execute('PRAGMA integrity_check').fetchone()[0]
   assert ok == 'ok', ok
   for db in ('/data/mig.db', '/data/agent-t-reviews.db'):
       c = sqlite3.connect(db)
       print(db, 'reviews', c.execute('SELECT COUNT(*) FROM reviews').fetchone()[0],
             'llm_calls', c.execute('SELECT COUNT(*) FROM llm_calls').fetchone()[0])"
   ```
   快照与源的 reviews/llm_calls 数**不一致即中止**（保留旧容器与全部
   原件，向老板报备）。
6. **重采基线**：§2.3.5⑤ 的快照数字记入执行日志（不沿用历史 1/14）
7. **贯通证据**（零付费）：同库贯通由 `test_same_db_end_to_end`（真
   SQLite 全链：种单→应用读单→应用写额度→runner 查到→清理后两边
   消失）在合并前证明；线上由步骤 9 接续核验兜底——两者如实分列，
   不互充
8. `docker rm agent-t`（**步骤 9 核验通过前不删**）→ `docker run`
   新容器（挂卷 + `STORE_DB_PATH=/data/agent-t-reviews.db` + 既有 env，
   镜像 `agent-t:<SHA>`）
9. **切换后核验**：应用内 reviews/llm_calls 数与 §2.3.6 一致 → 通过后
   才清 mig.db* 原件、才允许删旧容器镜像引用；此后数据均落宿主卷

**失败分支（按阶段，v5 细化）**：步骤 1-3 失败 → 未停机，放弃换装
（零影响）；步骤 4 失败（未排空）→ `docker start agent-t` 复活原服务
（数据未动）；步骤 5-7 失败（迁移/核验不过）→ 旧容器**已停未删**，
`docker start agent-t` 复活 + 删除 /data 半成品，原 /tmp 库原样保留；
步骤 8-9 失败 → 旧镜像 95555ed + 同一 env/卷重启（部署后新写入保留）。
**无双写**：旧容器 rm 后才起新容器。**停机窗口**=步骤 4→8（分钟级，
计划内）。

## 三、程序行为（按真实代码路径，v2 已修正并经复核确认）

四表均首次触库惰性 CREATE；词族首次检索时 lru_cache 加载（fail-closed
在调用时）。零付费证据：`test_families_yaml_fail_closed` + 新路由 404
探针（不触库不触配置）。

## 四、回滚预案（v3 修正）

- **触发**：新容器启动失败/安全门不过/数据接续核验不过
- **动作**：`docker rm` 新容器 → 用 **95555ed 旧镜像 + 同一 env
  （含 STORE_DB_PATH）+ 同一宿主卷**重启——#148 版已支持 STORE_DB_PATH
  （只读核过 store/revision 代码路径），旧代码读同一迁移后库，
  **部署后新写入保留**
- 恢复备份库仅限灾难场景（库损坏/污染），执行前向老板报备

## 五、部署验收（换装后必跑）

1. **零成本项**：安全门六连；版本比对（镜像标签=冻结 SHA 前缀）；
   新路由 404 探针；数据接续核验（§2.3.9）
2. **业务流验收**（可能产生真实模型费用，见 §六）：上传 → 审查结果 →
   导出报告（PK 魔数）→ 追问可用

## 六、验收费用（v4：可复算公式+预算两案呈批+实付口径勘正）

**复算公式**（阀门=代码登记常量；牌价=Flash 峰值 输出 ¥8/M，估算非实付）：

| 节点 | 触发条件（生产 env 实探） | 次数×输出阀门 | 上界输出 |
|---|---|---|---|
| model_review | DEEPSEEK_API_KEY 在位 | ≤6×13000 | 78k |
| quality | QUALITY_ENABLED 未显式关（默认开） | ≤6×25000 | 150k |
| category_suggest | CATEGORY_SUGGEST_ENABLED=开（实探在） | 1×16000（默认阀门） | 16k |
| ask | 验收追问 ≤2 次 | 2×8000 | 16k |
| objection | 如追问链触发（保守计入） | ≤2×10000 | 20k |
| precheck | 生产无开关 env（默认关），不计 | — | — |
| **合计上界** | | **280k → 峰值 ≈ ¥2.24**（输入另计 <¥0.1） | |

**预算两案（上界 ¥2.24+ 已超现控值 ¥2，按例预留语义下须先定案）**：

- **方案 A（开发团队推荐，codex 亦倾向）**：验收预算控制值调至 **¥3/次
  业务流**（按例预留——上界 ¥2.24+输入余量仍被覆盖）——质量层真实链路
  在验收中**被覆盖**
- **方案 B**：验收上传时显式 `QUALITY_ENABLED=false` 缩范围（上界降至
  ¥0.80 内，¥2 控值够用）——代价：业务流验收**不含**质量层真实链路，
  且改动生产开关本身也是一项变更
- **两案覆盖差异如实记录如上；⚠️ 未经老板明确选择前不执行业务流验收
  （沉默≠审批，不设默认）**

**执行机制（非纸面）**：预留=换装后首笔业务流请求发起前按 §六公式计
算本例上界，校验「DeepSeek 余额 ≥ 控制值」且累计圈账 < 控制值（同
runner `_can_afford_next` 语义）；逐节点停止=严格圈账（缺行/缺用量/
查询失败一律费用未知即停，工具与测试已在备料批落地：
`STRICT_SPENT_CODE`/`test_same_db_end_to_end` 等零付费证据）；¥2.24+
为估算，**非严格现金上限**，实付以供应商账单为准。

**历史锚点（勘正，以 docs/m65/final-replay-report-20261024.md 为准）**：
M6.5 回测 19 项（17 付费审查单+2 靶向证据）**峰值保守估算 ¥5.62、余额差
实扣 ¥2.68**——v3 误把 ¥5.62 写成实付，已改。实际通常远低于阀门上界。

**独立结算与停止**：验收样本单独 review_id，用 runner 同款严格圈账
（STRICT_SPENT_CODE+按节点核账）单独结算汇报；失败不重试不追跑。
**与五题考试分开记账分开授权**。

## 七、授权边界

本单=备料+核实+方案。换装待老板授权；业务流验收费用含在部署授权内
（§六）；五题考试另行授权。
