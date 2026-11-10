# Claude Code 开发前交接：账户任务恢复的单条坏记录隔离与公平推进

日期：2026-11-10。前序：`docs/handoff-20261110-user-relay.md`（分工）、`docs/handoff-20261110-owned-recovery-after.md`（其「未完成及风险」第一条即本项）。**仅本机合成数据，随机临时 SQLite，零模型密钥；不部署。**

## 一、复现结果（漏洞实锤）

复现脚本：`scripts/_repro_bad_record.py`（取证后随本轮删除或转正为测试，见下）。场景：alice 的审查 `data` 被写成非法 JSON（模拟历史版本写入或损坏），bobby 的健康任务紧随其后；两账户同批撤权失效。实测输出：

```
第一轮 recover_once 抛错: JSONDecodeError Expecting property name enclosed in double quotes...
坏任务状态: running ｜健康任务状态: running
健康审查 data.status: processing
第二轮同样抛错: JSONDecodeError —— 饥饿实锤
```

## 二、根因（读代码定位，`app/services/owned_review_jobs.py` `reap_stale`）

1. **整批一个事务**：`BEGIN IMMEDIATE` 包住全部被选记录的清理，任何一条抛错（`json.loads` 的 `JSONDecodeError`、审查行缺失的 `RuntimeError`（归属无效）、`data.status != "processing"` 的 `RuntimeError`（状态无效））→ **全批回滚**，连健康记录的清理一并撤销。
2. **坏记录每轮重新入选**：坏记录的任务行保持 `running` 且恒为 stale，按 `ORDER BY created_at` 永远排在批次窗口最前——每轮都先炸，健康记录**结构性饿死**（复现第二轮证实）。
3. **异常升级到顶层**：`recover_once` → `run()` 只记异常类型，30 秒后重试同一批，死循环。

## 三、修复方案（按交接要求的四个约束设计）

1. **事务回滚（单条粒度）**：每条记录用 `SAVEPOINT reap_one` 包裹——解析/清理失败时 `ROLLBACK TO SAVEPOINT` 只回滚该条，`RELEASE` 后继续下一条；整批事务仍原子提交（健康记录的清理不因坏记录丢失）。
2. **公平推进（防饿死）**：单条处理失败后**隔离该任务**——任务行本身可读可写，将其 `status` 置为 `failed`（该行随后不再被 stale 查询选中，不占窗口）。审查 `data` 一律**不猜测改写**：损坏（不可解析）自不必说；可解析但状态不符时也不能写 `error`——状态可能已 `done`（内容完整），贸然覆盖会误毁已完成的审查。两种情况都只隔离任务行、审查行原样留给离线迁移人工核查。坏记录数量超过窗口（LIMIT 全是毒）时，因失败者已隔离、下轮窗口自然推进，健康记录不饿死。
3. **权限与外发未知状态**：隔离动作只动本库任务行状态，不触碰 `owned_llm_attempts` 的 unknown 语义（该语义只在 `reconcile`，两者口径不变）；坏记录关联的 pending 调用仍由账本 reconcile 按「未知」处理——隔离不新增「未发送/已发送」的推断。
4. **隐私与可观测**：失败日志只记异常类型与任务 id（不含合同内容/凭证，与现有「失败日志只记录异常类型」口径一致）；新增隔离计数返回，便于告警接线。
5. **账本 reconcile 顺带堵一个非阻断缺口**：JOIN 不到（任务/审查行已消失）的孤儿 pending 行会永远不被标记 unknown——补一条孤儿 UPDATE（同事务内、同 unknown 语义），配测试。

## 四、本轮测试计划（全部合成数据）

1. 损坏 JSON + 健康任务同批：健康任务被正常清理、坏任务行被隔离为 `failed`、损坏的审查行字节原样未动、第二轮 `recover_once` 干净返回 (0, 0)。
2. 状态不符记录（可解析但 `status != processing`）：审查写入 error、任务隔离。
3. 审查行缺失的任务行：任务隔离，无异常外抛。
4. 公平推进：窗口 limit 内全是毒记录、健康记录在窗口外 → 数轮内健康记录仍被推进（直接调 `reap_stale(limit=1)` 三条毒+一条健康验证）。
5. 账本孤儿 pending → unknown。
6. 既有 12 项维护/账本/上传测试回归 + Ruff。

**明确不做（本轮）**：损坏审查行的自动修复（留给离线迁移）；多进程争锁实测；真实模型/网络；全量回归（按交接「适当专项」口径，跑相关专项并如实单列未跑项）。
