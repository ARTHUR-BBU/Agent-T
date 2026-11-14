# Claude Code 交接：账户上传接口阶段结果

## 已实现

- 增加默认关闭的本机账户上传试验接口，登录及 CSRF 校验后接收文件或粘贴文本，限制大小、并发和请求频率。
- 上传串接账户归属任务、服务端预审、单次确认票据和后台审查；确认时重新校验用户、内容与规则版本。账户模式关闭旧共享合同 API。
- 取消请求后后台线程持有并发名额直至退出；线程无法启动时尝试将任务标记失败。

## 验证

- 隔离数据库、禁用真实模型密钥：`python -X utf8 -m pytest -q tests/test_owned_upload_integration.py tests/test_local_owned_upload_cancellation.py tests/test_local_accounts_api.py tests/test_local_owned_reviews_api.py`：13 passed in 23.57s。
- `python -m ruff check app/api/routes_local_owned_upload.py app/api/routes.py app/main.py tests/test_owned_upload_integration.py tests/test_local_owned_upload_cancellation.py`：通过。
- 独立安全审核未发现新的可复现越权；确认异常任务恢复尚有缺口。

## 未完成及后续

- `OwnedReviewJobs.reap_stale` 和 `OwnedLLMLedger.reconcile` 尚无启动或周期调度：任务中途撤权、会话或租约失效时，失败状态和未知外发结果可能无法自动恢复。需设计受控调度、关停、批量及故障恢复测试，不能据此宣称正式可用。
- 取消检查与后台启动之间仍有竞态；真实浏览器断线不一定等同协程取消。确认票据仅在进程内，重启或确认后响应丢失缺少幂等恢复。
- 账户上传页面、账户归属的报告和问答等流程、规则正式发布与回滚完整链路仍待开发或验证。尚未进行真实模型、浏览器端到端、上线环境验证，也未部署。
- 后续测试必须使用独立临时 `STORE_DB_PATH` 和禁用真实模型密钥，不能污染本地真实数据。
