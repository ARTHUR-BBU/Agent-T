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

### 2.3 切换序列 v6（隔离入口可逆+在线排空+停机零 exec+旧容器改名保留）

**三个 v5 缺陷的本版解法**：
- 停机后不能 `docker exec` → WAL/SHM 存在性检测**前移到停机前在线
  exec**（停机后文件集冻结，按记录拷贝即可）；排空核查也在**在线**
  阶段完成（停机前确认无 processing，进程停止才等于工作完成）
- 技术阻断新上传 ≠ 停机 → **服务器防火墙临时封 8080 入站**（iptables
  DOCKER-USER 链 REJECT，可逆：删规则即恢复；旧容器照常运行，在途
  后台审查自然完成）
- 旧容器保留与原名冲突 → 停旧容器后 `docker rename` 为
  `agent-t-old-<ts>`（文件系统=原件 /tmp 库保留在内），新容器用原名；
  **核验通过才删旧，失败改回原名复活**

按序执行，任一步失败走本节末尾失败分支：

1. **预告**：低峰窗口，伙伴群通知短暂不可用时段
2. **预备份**（在线 backup API → 宿主卷，双保险非迁移源）+ 宿主卷
   `mkdir -p /opt/agent-t-data && chown 1000:1000` 先行
3. **构建镜像**：`git archive <冻结SHA>` → scp → `-f Dockerfile.server`
   → `agent-t:<SHA>`——构建期间旧容器照常服务
4. **隔离入口（技术阻断新上传）**：ssh 在服务器执行
   `iptables -I DOCKER-USER -p tcp --dport 8080 -j REJECT`
   （可逆：解除= `-D DOCKER-USER` 同款删规则；旧容器照常运行，伙伴侧
   新上传被拒，在途后台审查自然继续完成）。**记录已加规则**，任何
   后续中止都必须先解除封锁再收尾。**加完必须外部实测命中**
   （v7 依七轮复核补）：本地 `curl -m 10 http://<IP>:8080/health`
   必须失败（超时/拒绝）=隔离成立；本机运维 ssh 通道不受影响；
   解除规则后复测 /health 恢复 200。实测不成立 → 换封锁手段（如
   安全组）再验证，不带病前进
5. **在线排空**（此时 exec 可用）：轮询真实结构（每 30s）：
   ```bash
   docker exec agent-t python3 -c "
   import sqlite3, json, sys
   c = sqlite3.connect('/tmp/agent-t-reviews.db')
   bad = []
   for rid, created, data in c.execute('SELECT id, created_at, data FROM reviews'):
       try:
           d = json.loads(data)
       except ValueError:
           bad.append((rid, 'malformed'))  # 解析不了=无法证实已完成
           continue
       if d.get('status') == 'processing':
           bad.append((rid, 'processing'))
   if bad:
       print('NOT_DRAINED:', bad); sys.exit(1)
   print('drained')"
   ```
   排空（退出 0）才继续；**超 10 分钟未排空 → 解除封锁+报备放弃**
   （后台任务无法自然结束=不迁移，旧数据保持）
6. **在线记录文件集**（停机前最后一步 exec，之后文件集冻结）：
   ```bash
   docker exec agent-t sh -c \
     'if [ -f /tmp/agent-t-reviews.db-wal ]; then echo WAL_EXISTS; else echo WAL_ABSENT; fi;
      if [ -f /tmp/agent-t-reviews.db-shm ]; then echo SHM_EXISTS; else echo SHM_ABSENT; fi'
   ```
   **exec 失败（rc≠0）即中止**——执行故障不得记成「不存在」；结果
   （EXISTS/ABSENT 两条）记入执行日志
7. **停机**：`docker stop -t 60 agent-t`——此时在途已排空（步骤 5）、
   新上传已被封（步骤 4），进程停止即工作完成；此后文件集零写入
8. **终迁移（全部经 docker cp+临时容器，对已停容器零 exec）**：
   ```bash
   # ① 按步骤 6 记录拷文件集（v7 修正：WAL/SHM 实际拷贝命令——按在线
   #    记录存在则必须执行且必查返回码，失败即中止；SHM 按 SQLite 恢复
   #    语义拷出留档、恢复不参与。演练证明：缺 WAL 的主库连表都读不到）
   docker cp agent-t:/tmp/agent-t-reviews.db /opt/agent-t-data/mig.db
   # 步骤 6 记录 WAL_EXISTS 时，以下两条必跑且 rc 非零即中止（set -e）：
   docker cp agent-t:/tmp/agent-t-reviews.db-wal /opt/agent-t-data/mig.db-wal
   # 步骤 6 记录 SHM_EXISTS 时拷出留档（恢复不参与）：
   docker cp agent-t:/tmp/agent-t-reviews.db-shm /opt/agent-t-data/mig.db-shm
   # ② 权限前移（root cp 落 root 属主，先修才可被 uid1000 读）
   chown -R 1000:1000 /opt/agent-t-data
   # ③ 读探针（appuser 真能读，权限错误不得误判为文件缺失）
   docker run --rm -v /opt/agent-t-data:/data agent-t:<SHA> python3 -c "
   import os
   need = ['mig.db']
   if os.path.exists('/data/mig.db-wal'):
       need.append('mig.db-wal')
   for f in need:
       open('/data/'+f, 'rb').read(1)
   print('read ok')"
   # ④ backup API 收敛单文件 + 完整性 + **源/快照行数 fail-closed 比较**
   docker run --rm -v /opt/agent-t-data:/data agent-t:<SHA> python3 -c "
   import sqlite3, sys
   src = sqlite3.connect('/data/mig.db')
   dst = sqlite3.connect('/data/agent-t-reviews.db')
   src.backup(dst); dst.close(); src.close()
   ok = sqlite3.connect('/data/agent-t-reviews.db').execute('PRAGMA integrity_check').fetchone()[0]
   assert ok == 'ok', 'integrity: '+ok
   def counts(p):
       c = sqlite3.connect(p)
       return (c.execute('SELECT COUNT(*) FROM reviews').fetchone()[0],
               c.execute('SELECT COUNT(*) FROM llm_calls').fetchone()[0])
   a, b = counts('/data/mig.db'), counts('/data/agent-t-reviews.db')
   assert a == b, f'快照与源不一致：{a} != {b}'
   print('migrated', b)"
   ```
   assert 失败=退出码 1=**中止**（不靠人工看 print）；基线数字
   （「migrated ...」）记入执行日志
9. **新旧交接**：`docker rename agent-t agent-t-old-<ts>`（旧容器停用
   **未删**，文件系统原件=/tmp 库保留在内）→ `docker run` 新容器用
   **原名 `agent-t`**（挂卷 + `STORE_DB_PATH=/data/agent-t-reviews.db`
   + 既有 env，镜像 `agent-t:<SHA>`）
10. **切换后核验**：安全门六连+版本比对+新路由 404 探针+应用内
    reviews/llm_calls 数=§2.3 步骤 8 基线 → **通过**：`docker rm
    agent-t-old-<ts>`、清 mig 原件、解除步骤 4 封锁；**失败**：见失败
    分支③

**失败分支（按阶段，v7 按演练证据修正）**：
- ① 步骤 1-3 失败 → 未隔离，放弃换装（零影响）
- ② 步骤 4-7 失败（未排空超时/迁移 assert 失败/任何中止）→ **删封锁
  规则恢复原服务**；旧容器完好（停用未删时 `docker start` 复活），
  原件保留，报备——此阶段尚无新写入，直接复活即完整恢复（演练阶段
  2 证明原件未动）
- ③ 步骤 9 失败 → **法 B（演练验证保新写入）**：`docker rm agent-t`
  （新容器）→ **把宿主卷当前库拷回旧容器**：
  `docker cp /opt/agent-t-data/agent-t-reviews.db agent-t-old-<ts>:/tmp/agent-t-reviews.db`
  → `docker rename agent-t-old-<ts> agent-t` → `docker start` → 删封锁
  规则 → 报备。（直接复活旧容器**不拷库**=读旧 inner 库丢部署后新
  写入——演练法 A 已证伪，仅限步骤 7 前尚无新写入阶段使用）
  或等价：继续用旧镜像 95555ed+同一 env/卷+STORE_DB_PATH 重启
  （#148 已支持），同样保住新写入
**无双写**：旧容器已停（步骤 7 起）且新容器用原名，全程单写者。
**停机窗口**=步骤 7→9（分钟级，计划内）。

**演练证据**：以上 WAL 迁移/核验/回退三段已于 2026-10-30 在生产服务器
临时目录演练通过（零付费、未触碰生产容器），逐阶段记录见
`docs/m7/deploy-rehearsal-b72.md`。

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
   新路由 404 探针（断言 handler 级 detail——如 `review_not_found`/`draft_not_selected`，以区别旧镜像「路由整体不存在」的 FastAPI 默认 `{"detail": "Not Found"}`：仅 404 状态码不足以证明新路由已部署）；
   数据接续核验（§2.3 步骤 10）
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
| **precheck** | **PRECHECK_ENABLED 代码默认=true（生产未设 env，v8 勘正——验收实测 1 次在账）** | 1×3000 | 3k |
| **合计上界** | | **283k → 峰值 ≈ ¥2.26**（输入另计 <¥0.1） | |

**预算两案（上界 ¥2.26+ 已超现控值 ¥2，按例预留语义下须先定案）**：

- **方案 A（开发团队推荐，codex 亦倾向）**：验收预算控制值调至 **¥3/次
  业务流**（按例预留——上界 ¥2.26+输入余量仍被覆盖）——质量层真实链路
  在验收中**被覆盖**
- **方案 B**：验收上传时显式 `QUALITY_ENABLED=false` 缩范围（上界降至
  ¥0.80 内，¥2 控值够用）——代价：业务流验收**不含**质量层真实链路，
  且改动生产开关本身也是一项变更
- **两案覆盖差异如实记录如上；⚠️ 未经老板明确选择前不执行业务流验收
  （沉默≠审批，不设默认）**

**执行机制（v6 如实限定：事后核账，非逐节点熔断）**：现有工具证据
（`STRICT_SPENT_CODE`+`test_same_db_end_to_end`）证明的是**账本片段与
同库 mechanics**，不证明「上传→后台多节点→追问」每一节点前受熔断
控制——主审查链是既有生产代码，本次换装不改动它（插熔断=超范围变更）。
因此业务流验收的费用控制为：

1. **单样本**：验收只跑一条完整业务流（独立 review_id）
2. **事后严格核账**：跑完即按该单号严格圈账（缺行/缺用量/查询失败=
   费用未知）——工具与编排级测试已在备料批落地（零付费证据在案）
3. **停止规则**：费用未知 / 实算 ≥ 控制值（A=¥3、B=¥2，由老板明确
   选择）→ **不再跑任何后续验收项**并向老板报备；失败不重试不追跑
4. ¥2.26+ 为估算，**非严格现金上限**，实付以供应商账单为准

（逐节点熔断如需具备，属主审查链的独立变更，另行立项不在本次部署。）

**历史锚点（勘正，以 docs/m65/final-replay-report-20261024.md 为准）**：
M6.5 回测 19 项（17 付费审查单+2 靶向证据）**峰值保守估算 ¥5.62、余额差
实扣 ¥2.68**——v3 误把 ¥5.62 写成实付，已改。实际通常远低于阀门上界。

**独立结算与停止**：验收样本单独 review_id，用 runner 同款严格圈账
（STRICT_SPENT_CODE+按节点核账）单独结算汇报；失败不重试不追跑。
**与五题考试分开记账分开授权**。

## 七、授权边界

本单=备料+核实+方案。换装待老板授权；业务流验收费用含在部署授权内
（§六）；五题考试另行授权。

## 八、执行时核验关卡（七轮复核收口清单，换装现场逐条打勾）

以下三条为**换装执行时**的现场关卡（不再是纸面预案），任一不过即中止
并恢复原服务、向老板报备：

1. **迁移核验以停机后实际文件集为准**：WAL/SHM 以停机后宿主机上
   实际存在的文件为准（cp 前后 ls 记录），原库（mig.db+文件集）与
   backup 快照的 **reviews/llm_calls 记录数与 integrity_check** 双核对，
   不一致 → 保留旧容器（改名态），中止换装
2. **有新写入后的回退优先用「旧镜像+同一宿主卷」**：v7 的「拷主文件回
   旧容器」仅在确认新卷**无 WAL/已完成 checkpoint** 时使用（新卷仍有
   WAL 时该命令安全性未证明，演练样例不构成普适证明）——优先方案：
   `docker rm` 新容器 → 95555ed 旧镜像 + 同一 env（含 STORE_DB_PATH）
   + 同一宿主卷重启 → 核验部署后新记录仍在（对比 §2.3.6 基线）
3. **隔离与验收通道现场验证**：iptables 规则加后从本地 curl 实测外部
   新请求被挡（health 失败）、ssh 运维通道可用、（方案 A 时）带认证
   验收请求可达；排空/隔离任一无法证实 → 中止换装并恢复

**预算（待老板明确选择，沉默不执行）**：codex 建议并开发团队推荐
**方案 A（¥3 控制值，保留质量层完整验收）**——估算控制值非现金硬
上限，业务流单样本+事后严格核账；方案 B 仍可选项。五题付费考试
单独授权。
