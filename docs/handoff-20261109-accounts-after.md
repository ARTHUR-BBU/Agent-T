# Claude Code 交接：账户基础与规则治理（开发后）

日期：2026-11-09。用户要求继续真实规则启停、具名审核、发布和回滚；允许管理员审核自己提出的规则，必须记录并标示自审。暂不部署，留待 Claude Code 处理。开发前背景见 `handoff-20261109-accounts-before.md`。

## 本次完成

- 新增默认关闭的本机账户试验：独立 SQLite 账户、两类角色、密码哈希、服务端会话、停用和角色变更撤销会话、最后管理员保护、登录限制和账户操作记录。首位管理员通过 `python -m app.services.account_bootstrap` 离线交互创建，不提供公开初始化接口；需设置独立绝对路径，不要使用合同或规则草稿数据库。
- 新增本机试验 API、CSRF 与来源检查，以及拒绝无主或非本人合同、规则动作按角色检查的授权基础函数。账户试验使用独立 cookie 和数据库，默认不启用；无默认账号。
- 用户明确允许管理员自审，授权基础函数能标识自审，但业务审批记录与展示尚未接入。独立审核指出的 HTTP 首位管理员抢注问题已删除该入口；Cookie 缩窄路径仍不能实现浏览器跨端口隔离。
- 新增需求、威胁模型及测试。账户与规则方案见 `prd-accounts-permissions.md`、`threat-model-accounts.md`、`plan-rule-management.md`。

## 必须继续完成

1. 账户模块目前不是应用的正式登录：原有合同、附件、报告、对话、异议和规则草稿接口没有个人隔离；不得宣传多人安全或暴露公网。旧数据没有可信所有者，不可自动分配。
2. 在正式启用前补齐独立 origin/HTTPS/Secure Cookie、会话与数据访问隔离、凭据恢复、存储权限与迁移、审计保留和安全审核。当前本机试验不能经反向代理当作生产鉴权，Cookie 路径也不能隔离同主机其他端口。
3. 将实际规则提交、具名审核和自审标记接到可信身份及数据库；权限复核与状态写入须同事务。实现候选版本与测试证据绑定、篡改或基线变化后审批失效、正式启停、原子发布与回滚，以及历史审查锁定所用版本。做好并发、越权及反向测试。
4. 新账户表仍属开发阶段，尚无生产迁移方案；试验账号与审查数据不要混用。不要覆盖工作区现有改动及 `trial-db/`。

## 审核

独立子 agent 复核未发现新的可复现认证绕过；指出同主机其他端口仍可能接收匹配路径的 Cookie，生产设计必须解决。定向回归 `python -X utf8 -m pytest tests/test_account_bootstrap.py tests/test_local_accounts_api.py tests/test_account_store.py tests/test_access_policy.py tests/test_rule_drafts.py tests/test_rule_snapshot.py tests/test_rule_drafts_frontend.py -q`：58 passed，退出码 0。新增文件 Ruff 和账户基础 Pyright 均通过。全量回归启动后主动中断，未完成，不能报告全量通过。
