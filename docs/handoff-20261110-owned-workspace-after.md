# Claude Code 交接：账户合同工作区阶段结果

## 已实现

- 全部账户上传试验开关开启时，首页进入独立账户工作区：登录、选择试验品类和立场、上传文件或粘贴文本、本人记录、查看只读检查项。账户和规则管理保留独立入口。
- 品类建议需用户选择；切换时提示中性立场。确认票据和合同输入只保存在内存中，确认重新提交原内容；不拼接不可信 HTML。
- 退出或会话变化时清空输入、票据、结果与类别。请求绑定当前会话及代次，迟到结果不应覆盖新账户；类别加载失败时禁用提交。跨标签会话变更通过 BroadcastChannel 通知。
- 独立审核提出的旧上传跨会话继续执行、首次会话初始化与登录竞争、类别加载失败保留旧选项等问题已修复。

## 验证

- 隔离临时数据库、无真实模型密钥：`python -X utf8 -m pytest -q tests/test_owned_workspace_frontend.py tests/test_owned_upload_integration.py tests/test_accounts_frontend.py`：6 passed in 47.61s。包括真实本机服务与 Chromium 登录、粘贴虚构合同、审查结果和退出清空；模拟确认、安全文本、无可用品类及上传权限。
- `node --check app/static/owned_workspace.js` 和 `node --check app/static/accounts.js` 均退出 0；`python -m ruff check app/main.py tests/test_owned_workspace_frontend.py` 通过。

## 尚未完成及注意

- 此工作区仅显示账户拥有的检查项只读结果；旧工作台的报告导出、问答、修改建议等尚未完成账户授权接线。账户与规则管理入口仍使用现有试验页面，规则正式发布、回滚及完整用户管理仍需完成并验收。
- 浏览器实测使用虚构文本和关闭预审；文件上传、真实模型、不同标签页竞争、网络故障和不同屏幕尺寸尚未完整实测。跨标签广播不可用时，依赖服务端会话和 CSRF 拒绝旧操作。
- 未解决恢复任务损坏记录阻塞整批等既有问题；尚无当前全量回归和生产环境证明，没有部署。工作区只限本机测试。
