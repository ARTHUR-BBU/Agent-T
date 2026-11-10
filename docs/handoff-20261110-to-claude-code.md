# 给 Claude Code 的工作交接：规则与用户管理

日期：2026-11-10。用户要求把当前金标进程控制和风险闸这段做完后交接；继续推进规则管理、用户管理的开发和回归。**当前仅限本机测试，暂不部署**。本报告是当前交接入口；此前各 `handoff-20261110-*-after.md` 是各阶段时点记录，遇到状态不一致以当前代码和本报告为准。

## 工作区现状

- 项目：`F:\合同审查Agent\Agent-T`；分支 `b76-followup-recovery`；当前 HEAD `cbd9ee2 完善合同输入页并增加规则目录`。大量代码、测试、文档是未提交或未跟踪改动；它们不在该提交中。请先阅读 `git status --short`、逐项核对归属，不要 reset、clean、覆盖或擅自提交 `trial-db/`、`.env`、合同和其他数据。
- 用户之前要求开发前后均写交接，且开发者之外需要独立代码审核。当前批次开发前报告：`docs/handoff-20261110-gold-job-before.md`；阶段报告：`docs/handoff-20261110-gold-job-after.md`。本报告补充最新复测。
- 早期 `docs/handoff-20261110-process-tree-after.md` 写“尚未接入金标入口”是旧时点：**目前 Windows 品类金标已接入** `app/services/regression_process.py`，Linux 仍是旧 subprocess 路径。

## 当前已做

- 本机账户试验：账户、角色、会话、管理员操作，账户归属的上传、预审确认、后台审查及只读结果工作区。旧共享合同 API 在账户模式受限制；账户模式仍不是整个产品的生产多用户完成版，导出、问答、修改建议等尚未完整接线。详见 `docs/handoff-20261110-owned-workspace-after.md`。
- 本机规则试验：受限的现有检查项查看、候选新增/编辑/启停、金标、具名审核及默认关闭的版本历史、回滚和发布接口。发布接口仍要求真实完整候选回归记录，而**目前没有可信生产者和发布按钮**；测试里的完整回归记录是合成数据。规则历史页面只展示最近 100 条。详见 `docs/handoff-20261110-rule-release-api-after.md`。
- 候选规则隔离副本及真实 HTTP 文件规则路径验证已实现；这不是安全沙箱，也不是完整候选回归。详见 `docs/handoff-20261110-candidate-workspace-after.md` 和 `docs/handoff-20261110-candidate-http-after.md`。
- Windows 金标超时、残留或进程控制异常不计通过并保留临时工作区；同一账户 SQLite 中的持久风险闸旨在阻止后续金标、审核通过和发布。任何金标运行时也暂不允许新的审核通过或发布；退回和回滚仍允许。UI 对运行、故障和未知状态采取保守显示。租约过期收尾漏洞已修改，专项验证结果见下；真实多进程和离线恢复尚待验收。

## 交接后独立复核：P1 租约过期收尾漏洞与修复

`app/services/rule_validation_service.py` 的 `validate` 在结果返回后发现 `job["lease_until"]` 已到期时会抛异常；如果期间没有其他 worker 先回收，且进程控制没有报不安全，异常分支只将该 job 改为 `failed`，不会设置 `rule_validation_control.blocked`。之后新任务、审核通过或发布可能继续，和“过期任务需先检查再恢复”的预期不一致。

已使用系统临时目录中的合成 pytest 和独立临时 SQLite 复现：虚构金标运行期间将时间推进 601 秒，第一次因租约过期失败；随后数据库显示 `blocked=0`、任务为 `failed`，第二次验证成功启动并返回通过。命令退出码 0、**1 passed in 7.61s**；此通过代表漏洞复现成功，不代表产品正确。未修改项目代码。

Claude Code CLI（本机配置 GLM-5.3-flash）修改了 `app/services/rule_validation_service.py` 与 `tests/test_rule_validation_service.py`，调用因预算上限中止，没有完成报告；Codex 随后补充保留原风险事件来源的处理和异常边界测试。当前过期收尾在同一事务中落闸并标记 abandoned；落闸失败应回滚并保留 running。独立子 agent 静态复核了正常及异常收尾、并发回收与写闸失败路径。并发测试使用 SQL 模拟其他 worker，尚未证明真实多进程竞争；候选或账户失效先于租约检查的场景也仍需补测。

专项测试：隔离临时 SQLite、关闭 owned LLM 后运行 `python -X utf8 -m pytest -q tests/test_rule_validation_service.py`，19 passed in 225.46s，退出码 0。首次运行 18 passed、1 failed，失败是旧测试期待 ValueError，但审核安全闸实际抛 RuntimeError；已修改断言并整份重跑。Ruff 检查上述两个 Python 文件通过。未重跑当前版本全量回归；未完成真实多进程、Linux 和真实发布/回滚端到端验收。

## 本批关键文件

- 金标与进程：`app/services/rule_candidate_validation.py`、`app/services/regression_process.py`。
- 事务风险闸与审核发布：`app/services/rule_validation_service.py`、`app/services/rule_validation_guard.py`、`app/services/rule_review_ledger.py`、`app/services/rule_release.py`、`app/api/routes_local_rule_management.py`。
- 页面：`app/static/index.html`、`app/static/rules_review.js`。
- 本批测试：`tests/test_rule_candidate_validation.py`、`tests/test_regression_process.py`、`tests/test_rule_validation_service.py`、`tests/test_rule_release.py`、`tests/test_rule_review_frontend.py`。

## 实际验证与失败记录

- 本轮开始前，Windows 无真实模型密钥的软件基线：`python -X utf8 -m pytest -q` 为 **1863 passed、34 skipped，1069.98 秒，退出码 0**。此后代码和测试已修改，不能当作当前版本的全量回归；命令未逐条输出 34 个跳过原因。
- 本批早期组合曾 **46 passed，303.32 秒**；其后守卫、API、页面及测试继续修改，不是最终文件版本的完整专项。
- 补上发布遇 `running` 用例后，服务器风险闸筛选 **7 passed、15 deselected，128.76 秒**；浏览器风险状态筛选 **5 passed、8 deselected，92.20 秒**；退出码均为 0。两个命令同时运行，机器负载较高。
- 并行启动的金标/进程/API 组合出现 **4 failed、12 passed，退出码 1**：首个真实金标结果不通过后触发进程内安全闸，两个后续用例因此拒绝；另一个 4 秒超时用例未看到启动标记。独立审核并行组合也有该握手用例失败。不能断言失败仅因负载。
- 停止并行测试后串行重跑 `tests/test_rule_candidate_validation.py tests/test_regression_process.py tests/test_local_rule_management_api.py`：**16 passed in 265.07s，退出码 0**。之后只加强了该超时测试的孙进程 PID、最长等待和 finally 清理；最新版单项 `tests/test_rule_candidate_validation.py::test_actual_gold_timeout_stops_child_and_preserves_work` **1 passed in 29.39s，退出码 0**。该 PID 检查仍有极低的 PID 复用可能，不能证明所有资源均已释放。
- 本批相关 Python 文件 Ruff 与 `node --check app/static/rules_review.js` 通过；独立审核复核了权限、事务和页面状态，但没有独立运行最新版 PID 单项。所有测试应设置随机独立 `STORE_DB_PATH`，清空真实模型密钥，并避免同时运行多个耗 CPU 的金标组合。

## 请优先接续

1. **完成安全恢复与多进程验收**：实现有审计的离线检查/恢复流程；目前风险闸没有在线解除入口。验证真实双 worker 同时竞争、服务被强杀、租约过期后的持久阻断和恢复；不得仅因 Job 活动计数为零就认定所有资源释放。同库闸不协调不同数据库或不同主机。
2. **真实完整候选回归**：在隔离副本中分别验证软件基线、候选实际生效、正反例/评分和独立服务；明确收集、跳过与负对照。Windows 需验证整个受控进程树、异常保留工作区及资源释放；Linux 旧 subprocess/管道路径尚无同等保障。固定依赖/环境，禁止意外使用真实密钥及不受控外发。当前源码指纹并非不可变或签名构建证明。
3. **规则正式发布**：只有真实完整回归、同一候选的具名审核、正确代次与可信运行环境均完成，才能接通发布界面并开展完整发布/回滚验收；不要人工插入或伪造测试通过记录。
4. **用户管理和合同功能**：完成账户隔离的导出、问答、修改建议等路径与撤权/多标签页验证；处理账户任务恢复中坏记录阻塞整批及留存策略。不要声称试验账户工作区已达到生产多用户安全。
5. 最后在**当前最终代码**上运行适当专项、全量回归及独立审核；单列跳过项、真实模型、Linux 和部署验证。历史生产部署曾被自动审核以 `blocked by policy` 拒绝，用户随后明确暂不部署；不要绕过限制或把本机测试写成已上线。
