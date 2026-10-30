# B7-2 部署单（2026-10-30）

> 性质：**部署前备料**（老板 2026-10-30 指示「先准备部署单并核实生产当前版本，
> 确认目标版本、备份和验收步骤；授权部署后才执行换装」）。本单不含任何
> 换装动作；付费开考另行授权（¥0.59 峰值估算 / ¥2 按例预留预算控制值，
> 实际以供应商账单为准）。

## 一、版本核实（2026-10-30 实测）

| 项 | 值 | 核实方式 |
|---|---|---|
| 生产当前在跑 | `agent-t:95555ed` | ssh 只读探针（docker 镜像标签） |
| 生产健康 | /health 200 | 只读探活 |
| 95555ed 含什么 | B7-1 三版改写核心（#148）——B7-1 实跑时的版本 | 历史部署记录+老板指示核对项闭环 |
| 生产**没有**什么 | #153/#154（B7-2a/B7-2b 连带影响）/#156（备料批 runner，不进生产镜像） | 版本差异 |
| 目标版本 | **main @ `fb28e93`**（#156+#157 合并后 HEAD） | git log |
| 部署后新能力 | 草案选定/连带影响路由（`POST .../revision/select`、`POST .../revision/impact`） | 代码 |

## 二、变更面与兼容性

- **新表**：`selected_drafts`、`impact_reports`（均 `CREATE TABLE IF NOT EXISTS`
  启动时建，只增不删，符合账本/表结构向前兼容纪律）；既有
  `revision_drafts`/`revision_budget` 结构不变
- **新路由**：select/impact 两个 POST——不在主审查路径，不影响既有流程
- **新配置文件**：`config/impact_keywords.yaml`（词族，fail-closed 加载，
  结构错误启动即抛——重启循环排障提示见 SOP）
- **无 schema 破坏性变更**：常规回滚可用（见 §五）

## 三、换装步骤（授权后按序执行，SOP=docs/deploy-server.md）

1. **备份账本**（换容器前必做，B2-3 事故教训）：
   `docker cp agent-t:/tmp/agent-t-reviews.db /opt/backup/agent-t-reviews-<时间戳>.db`
2. **传代码**：`git archive --format=tar.gz fb28e93` → scp → 服务器解包
3. **构建**：`docker build -f Dockerfile.server -t agent-t:latest .`
   （必须 `-f Dockerfile.server`——根 Dockerfile 是 HF 演示镜，红线）
4. **换容器**：`docker rm -f agent-t` → 重跑 `docker run`（env 不变）
5. **env 核对**（变量名核对，勿回显值）：BASIC_AUTH_*、ZHIPU_API_KEY、
   **DEEPSEEK_API_KEY**（revision/impact 服务依赖——B7-1 实跑已验证在位，
   换容器后复查）、限频/预算变量不回退

## 四、部署验收（换装后必跑，全过才算完成）

1. **安全门**（SOP 标准六连）：/health 200；/、/api/categories、/docs 401；
   带认证 / 200；变体路径（//health 等）非 200
2. **版本比对**：caller HEAD（fb28e93）= 服务端镜像标签——不齐即部署失败
3. **新路由探针**（零成本）：
   `POST /api/review/{不存在单}/items/x/revision/select` → 404；
   同款 `/revision/impact` → 404——证明新路由已上线且未泄露
4. **完整业务流**：上传合同 → 审查结果 → 导出报告（PK 魔数）→ 追问可用
5. **留痕核对**：受控上传后 llm_calls 有行、reviews 落库

## 五、回滚预案

- **常规回滚**：回退到 95555ed（兼容版本，显式列名写入）或前向热修——
  新表只增不删，旧版本不读新表，无互斥
- **灾难恢复**（库损坏/数据污染才用，执行前必须向老板报备）：恢复 §三.1
  备份库

## 六、授权边界（如实声明）

- 本单=备料+核实，**未执行任何换装动作**
- 换装：待老板授权后按 §三 执行，执行完 §四 验收并汇报
- 付费开考：部署验收+版本比对通过后，**仍需老板单独授权**
