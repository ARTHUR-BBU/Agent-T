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

### 2.3 切换序列（P1-2：消除备份→停机写入空窗）

按序执行，**任何一步失败走 §四回滚**：

1. **预告停接入**：低峰窗口执行；窗口开始起不再有新上传（伙伴群通知+
   Basic Auth 不变仅停止主动使用）
2. **预备份**（旧容器在线时，backup API 一致性快照）→ 宿主卷就位
   （§2.2 权限先行）
3. **构建镜像**（`git archive <冻结SHA>` → `-f Dockerfile.server` →
   `agent-t:<SHA>`）——构建期间旧容器照常服务（不影响备份）
4. **排空**：`docker stop agent-t`（SIGTERM 等待退出；`docker ps` 确认
   已停；`docker logs` 尾部确认无处理中请求）
5. **终备份**（**从已停容器**，此点之后库内零写入）：
   `docker cp agent-t:/tmp/agent-t-reviews.db /opt/agent-t-data/agent-t-reviews.db`
   （容器已停无写者，主文件+WAL 静止，整体拷贝一致）→ `chown 1000:1000`
   迁入文件
6. **重采基线**：从迁入库读当前 reviews/llm_calls 具体数字（记入执行
   日志，不沿用 1/14 旧基线）
7. **贯通验证**（起临时容器，零付费）：
   `docker run --rm -v /opt/agent-t-data:/data agent-t:<SHA> python3 -c`
   读 reviews/llm_calls 数与 §2.3.6 一致 → §2.2 双探针复跑
8. `docker rm agent-t`（此时才删）→ `docker run` 新容器
   （挂卷 + `STORE_DB_PATH=/data/agent-t-reviews.db` + 既有 env，
   镜像 `agent-t:<SHA>`）
9. **切换后核验**：应用内 reviews/llm_calls 数与 §2.3.6 一致（接续
   成功）；此后新旧数据均落宿主卷

**无双写**：旧容器 rm 之后才起新容器，全程仅一个写者。**停机窗口**=
步骤 4→8（分钟级，计划内）。

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

## 六、验收费用（v3：可复算公式+停止机制，标注估算）

**复算公式**（阀门=#137 登记常量；牌价=Flash 峰值 输出 ¥8/M，估算非实付）：

| 节点 | 触发条件（生产 env 实探） | 次数×输出阀门 | 上界输出 |
|---|---|---|---|
| model_review | DEEPSEEK_API_KEY 在位 | ≤6×13000 | 78k |
| quality | QUALITY_ENABLED 未显式关（默认开） | ≤6×25000 | 150k |
| ask | 验收追问 ≤2 次 | 2×8000 | 16k |
| objection | 如追问链触发（保守计入） | ≤2×10000 | 20k |
| **合计上界** | | | **264k → 峰值 ≈ ¥2.11**（输入另计 <¥0.1） |

**口径声明**：上界≠预期——M6.5 历史实测约 ¥0.3/卷（回测 19 卷实付
¥5.62，账本可查），实际通常远低于阀门上界。**验收预算控制值 ¥2（按例
预留，与考试同口径；非现金硬上限）**；过程中任何异常或费用未知（严格
圈账：缺行/缺用量）即停。

**独立结算与停止**：验收样本单独 review_id，用 runner 同款严格圈账
（STRICT_SPENT_CODE+按节点核账）单独结算汇报；失败不重试不追跑。
**与五题考试分开记账分开授权**。

## 七、授权边界

本单=备料+核实+方案。换装待老板授权；业务流验收费用含在部署授权内
（§六）；五题考试另行授权。
