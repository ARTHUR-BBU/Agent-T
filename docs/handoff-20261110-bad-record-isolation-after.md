# Claude Code 开发后交接：账户任务恢复的单条坏记录隔离与公平推进

日期：2026-11-10。前序：`docs/handoff-20261110-bad-record-isolation-before.md`（复现+方案）。**仅本机合成数据；未部署；未跑全量回归。**

## 一、变更文件

| 文件 | 变更 |
|---|---|
| `app/services/owned_review_jobs.py` | `reap_stale` 单条 SAVEPOINT 隔离 + 隔离计数；新增 `logging`；构造器新增 `data_integrity_check` 参数（默认 True 不变，见下） |
| `app/services/owned_llm_ledger.py` | `reconcile` 补孤儿 pending 行（任务/审查行已消失）的 unknown 收口 |
| `app/services/owned_review_maintenance.py` | 恢复路径构造 `OwnedReviewJobs` 时传 `data_integrity_check=False` |
| `tests/test_owned_review_maintenance.py` | 新增 5 个针对性测试（含复现场景转正） |
| `docs/handoff-20261110-bad-record-isolation-before.md` | 开发前报告（复现实录） |
| `docs/handoff-20261110-bad-record-isolation-after.md` | 本报告 |

## 二、关键设计

1. **单条隔离（事务回滚粒度）**：`reap_stale` 每条记录包 `SAVEPOINT reap_one`——解析/归属/状态失败只 `ROLLBACK TO SAVEPOINT` 本条，整批事务原子性不变（健康记录的清理不随坏记录回滚）。
2. **公平推进（防饿死）**：失败记录的任务行置 `failed`（不再被 stale 查询选中、不占 LIMIT 窗口）；毒记录占满窗口时，隔离后下轮窗口自然推进到健康记录（测试 `test_poison_window_does_not_starve_healthy_behind` 锁定）。
3. **审查行一律不猜测改写**：损坏（不可解析）自不必说；**可解析但状态不符也不写 `error`**——开发前报告原设计是「可解析即安全写 error」，实现时发现反例：状态漂移可能意味着审查已 `done`（内容完整），覆盖会误毁成果。修正为两种情况都只隔离任务行、审查行原样留给离线迁移（开发前报告已同步修正，如实留痕）。
4. **第二道阻塞层的发现与处理**：恢复路径构造 `OwnedReviewJobs` 时，数据行级 `foreign_key_check` 会把损坏库整个挡在门外——恢复在最需要时跑不起来（复现路径实测）。处置：构造器新增 `data_integrity_check` 参数，**结构校验（DDL/列集/CASCADE 级联声明）对一切调用方保持严格**，仅数据行级扫描在维护恢复路径跳过——`reap_stale` 的隔离机制正是这类违例的自愈路径。默认值 True，create/commit 等常规路径行为零变化。
5. **外发未知状态不新增推断**：隔离动作只动任务行状态；坏记录关联的 pending 调用仍由账本 `reconcile` 按 unknown 处理（新增的孤儿收口同语义——任务/审查行已消失导致的永久 pending 也会被标 unknown，不推断是否已发送/计费）。
6. **隐私**：隔离日志只记任务 id + 异常类型，不含合同内容/凭证。

## 三、精确测试命令与结果

```
python -X utf8 -m pytest -q tests/test_owned_review_maintenance.py
→ 9 passed in 119.53s（含 5 个新测试 + 4 个既有）

python -X utf8 -m pytest -q tests/test_owned_review_maintenance.py tests/test_owned_llm_ledger.py \
  tests/test_owned_review_jobs.py tests/test_owned_upload_integration.py tests/test_local_owned_upload_cancellation.py
→ 30 passed in 297.96s（关联专项回归）

python -m ruff check app/services/owned_review_jobs.py app/services/owned_llm_ledger.py \
  app/services/owned_review_maintenance.py tests/test_owned_review_maintenance.py
→ All checks passed
```

新测试覆盖：①损坏 JSON 记录隔离+健康同批推进+损坏行字节原样+二轮干净返回；②状态不符隔离（审查 data 不被半改写）；③审查行缺失的任务行隔离（绕 FK 模拟历史损伤）；④毒窗口不饿死排后健康记录；⑤账本孤儿 pending → unknown。

## 四、未验证边界（如实单列）

1. **未跑全量回归**：交接基线 1863 passed 是旧版本，本批后未重跑——按交接「适当专项」口径只跑了上述关联专项。
2. **多进程争锁**：隔离在单事务内完成，多进程各有维护任务时靠 SQLite 写锁串行化；两个维护进程同时跑隔离批的场景未实测。
3. **隔离的可观测告警接线**：目前 `logger.error`（类型+任务 id），无告警通道；「需离线迁移核查」无界面入口。
4. **数据行级 FK 扫描跳过的安全复核**：仅维护恢复路径使用；请 Codex 重点审 `data_integrity_check=False` 的暴露面（是否有非维护代码路径能传 False）。
5. **损坏审查行的离线迁移工具**：本轮明确不做（隔离只是止血，人工核查路径不存在）。
6. 合同到期删除级联清账本的保留/计费证据策略、真实模型/网络、Linux——均沿用总交接挂账，本批未触碰。

## 五、给 Codex 的复核建议

重点三处：①`reap_stale` 的 SAVEPOINT 边界（`RELEASE`/`ROLLBACK TO` 配对、隔离 UPDATE 在保存点外的正确性）；②`data_integrity_check=False` 的调用面审计；③孤儿 pending 判据 SQL（`NOT EXISTS` 双条件是否恰好覆盖「任务行消失」与「任务在但审查行消失」两类，不多不少）。
