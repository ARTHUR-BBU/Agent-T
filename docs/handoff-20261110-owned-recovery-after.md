# Claude Code 交接：账户任务恢复阶段结果

## 已完成

- 在明确开启的本机账户上传试验生命周期中启动维护任务；启动立即检查，通常每 30 秒重试，满批时 1 秒后继续，每次每表最多 100 条。关闭时停止调度并等待正在执行的维护线程。
- 先将失效任务中未结算的模型调用标记为 unknown，再将失效任务标记失败；有效会话且租约未过期的任务保持运行。缺少调用账本的旧库仍可回收任务，不会为此创建账本。
- 仅打开已存在、符合账户模式路径隔离的数据库；失败日志只记录异常类型，不包含合同或凭证。独立审核提出的缺失账本问题已修复。

## 验证

- 隔离数据库、禁用真实模型密钥：`python -X utf8 -m pytest -q tests/test_owned_review_maintenance.py tests/test_owned_llm_ledger.py tests/test_owned_upload_integration.py tests/test_local_owned_upload_cancellation.py`：12 passed in 36.50s。覆盖撤权、有效任务、缺账本、未知外发、关停等待及真实账户模式应用生命周期启动。
- `python -m ruff check app/services/owned_review_maintenance.py app/api/routes_local_accounts.py app/main.py tests/test_owned_review_maintenance.py tests/test_owned_upload_integration.py`：通过。

## 未完成及风险

- 单条损坏记录或未知清理扩展可能使整批回滚并持续阻塞后续记录；目前只记录异常类型，缺少隔离与可观测告警。数据库锁可能延长关停等待，多进程各有维护任务，依靠数据库事务串行化。
- 调用标记 unknown 不代表未发送或没有费用。合同到期删除可能级联清除账本，保留期限和计费证据策略仍需明确。进程崩溃且会话有效的任务要等租约到期才能恢复。
- 尚未验证大于 100 条、损坏记录隔离、多进程争锁、真实模型或真实网络的完整场景；账户页面和规则发布回滚完整链路仍待完成，也没有部署或进行最新全量回归。
