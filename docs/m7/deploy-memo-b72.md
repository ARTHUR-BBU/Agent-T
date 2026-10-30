# B7-2 部署单 v2（2026-10-30，按 #158 复批修正）

> 性质：**部署前备料**（老板指示：先备部署单并核实生产，授权后才换装；
> 付费开考另行授权——¥0.59 峰值估算 / ¥2 按例预留预算控制值，实际以
> 供应商账单为准）。v2 修正：版本三方对齐、数据接续方案、惰性建表
> 表述、验收费用事前列明。

## 一、版本核实与三方对齐（P1-1）

**生产核实（2026-10-30 只读探针）**：在跑 `agent-t:95555ed`（B7-1 实跑版，
含 #148），/health 200。

**版本比对机制（已核代码 `tools/m65/final_replay.py`）**：`server_revision`
读容器 `.Config.Image` 的标签；`check_revision` 只接受「合法 Git SHA 且为
调用方全 SHA 前缀」——**`latest` 必挂**。

**三方对齐方案**（执行时落地，杜绝比对失败）：

| 项 | 规则 |
|---|---|
| 源码版本 | #158 合并后**冻结 main HEAD 全 SHA**（记入执行日志；不再是 fb28e93——合并 #158 后 HEAD 已变） |
| 镜像 | `git archive <全SHA>` 构建，标签 **`agent-t:<该SHA>`**（非 latest）；`docker run` 同标签镜像 |
| 考试工具 | 同一全 SHA 上运行（`git checkout <全SHA>` 或 HEAD 恰为该 SHA）；比对用 runner 同款 `server_revision`+`check_revision` 函数核验 |

**换装前置核对**：#158 必须先合并（部署单本身在 #158 里）。

## 二、数据接续方案（P1-2，探针实证后制定）

**生产实态（2026-10-30 只读探针）**：
- 挂载：`Mounts: [] / Binds: null`——**无宿主卷，库在容器内 /tmp**
- 库：`/tmp/agent-t-reviews.db`，**journal_mode=wal**
- 数据量：reviews=1、llm_calls=14、revision_drafts=0
- 结论：`docker rm -f` 会连库销毁；且 WAL 下仅拷主文件不含未检查点事务

**接续方案（借换装之机把持久化隐患一并修掉——B2-3 事故根源）**：

1. **一致性备份**（不用文件拷贝，用 SQLite backup API，保 WAL 已提交事务）：
   ```bash
   ssh root@<IP> 'docker exec agent-t python3 -c "
   import sqlite3
   src = sqlite3.connect(\"/tmp/agent-t-reviews.db\")
   dst = sqlite3.connect(\"/tmp/backup-migrate.db\")
   src.backup(dst)
   dst.close()
   print(\"backup ok\")"'
   docker cp agent-t:/tmp/backup-migrate.db /opt/agent-t-data/agent-t-reviews.db
   ```
   （宿主机先 `mkdir -p /opt/agent-t-data`；备份完成后再动容器）
2. **新容器挂宿主卷 + 显式库路径**：docker run 增加
   `-v /opt/agent-t-data:/data -e STORE_DB_PATH=/data/agent-t-reviews.db`
   ——审查单/账本/草稿/预算全部落到宿主卷（`store` 与 `revision` 均读
   `STORE_DB_PATH`，#148 版已支持），今后换容器数据天然接续
3. **换装后核验数据可见**：新容器内 `reviews=1、llm_calls=14` 与换装前
   一致（数字对上=接续成功）；此后新产生的数据亦在宿主卷
4. **回滚语义（修正）**：旧镜像 95555ed 同样支持 `STORE_DB_PATH`——回滚
   = 用旧镜像+**同一 env/同一卷**重启，部署后新写入保留；恢复备份库仅限
   灾难场景（须向老板报备）

## 三、程序行为描述修正（P2，按真实代码路径）

- `selected_drafts`/`impact_reports`/`revision_drafts`/`revision_budget`
  均为**对应操作首次触库时惰性 CREATE**（`revision._connect()`），非启动
  自动建——v1 表述有误，已改
- `config/impact_keywords.yaml` 由 `_load_families()` 在**首次检索时**
  lru_cache 加载，结构非法**在调用时**抛错（非启动校验）
- 零付费证据：`tests/test_b72b_impact.py::test_families_yaml_fail_closed`
  （调用时 fail-closed）+ 新路由 404 探针（§五.3，不触库不触配置）

## 四、换装步骤（授权后按序执行）

1. §二.1 一致性备份 → 宿主卷就位
2. `git archive <冻结全SHA>` → scp → 解包
3. `docker build -f Dockerfile.server -t agent-t:<该SHA> .`（红线：必须
   `-f Dockerfile.server`）
4. `docker rm -f agent-t` → `docker run` 新容器（既有 env + 新增
   `-v /opt/agent-t-data:/data -e STORE_DB_PATH=/data/agent-t-reviews.db`，
   镜像用 `agent-t:<该SHA>`）
5. **env 核对**（变量名核对勿回显）：BASIC_AUTH_*、ZHIPU_API_KEY、
   DEEPSEEK_API_KEY、限频/预算变量不回退、新增 STORE_DB_PATH

## 五、部署验收（换装后必跑）

1. **安全门六连**（零成本）：/health 200；/、/api/categories、/docs 401；
   带认证 / 200；变体路径非 200
2. **版本比对**（零成本）：runner 同款函数核验「镜像标签=冻结 SHA 前缀」
3. **新路由 404 探针**（零成本）：select/impact 打不存在单 → 404
4. **数据接续核验**（零成本）：reviews=1、llm_calls=14 与换装前一致
5. **完整业务流**（**可能产生真实模型费用，与五题考试分开**，见 §六）：
   上传合同 → 审查结果 → 导出报告（PK 魔数）→ 追问可用

## 六、验收费用事前列明（P2）

| 项 | 说明 |
|---|---|
| 触发面 | 业务流验收的上传审查（model_review/quality/objection 等 LLM 节点，生产带 Key 即真实调用）+ 追问（ask 节点，1-2 次） |
| 费用口径 | 按 #137 阀门常量 + Flash 牌价峰值估算（同 M6.5/费用单口径，估算非实付） |
| 预算边界 | 单次完整业务流峰值估算 **< ¥1**（审查链全节点开 + 2 次 ask）；超过即异常停止 |
| 圈账 | 按验收样本 review_id 精确圈账，与五题考试（manifest-b72 的 5 个单号）**分开记账分开汇报** |
| 停止标准 | 任何节点费用未知/账本缺行缺用量即停；业务流失败不重试不追跑 |
| 授权 | 部署授权含本业务流验收费用；**五题考试付费另行授权**（两笔独立事项） |

## 七、授权边界

- 本单=备料+核实+方案，**未执行任何换装**
- 换装待老板授权；业务流验收费用含在部署授权内（§六）；五题考试另行授权
