# Claude Code 交接：金标入口进程管理阶段结果

## 已实现

- Windows 品类金标入口改用受控 Job 运行器；超时、残留子进程或控制异常不记为通过。未捕获 stdout 时返回 `output_sha256=null`、`output_capture=discarded`，不伪装原始输出摘要；测试结果文件按 1 MiB 限额读取并校验结构。
- Windows 超时、残留或进程控制异常时保留临时工作区，并阻断当前进程的新金标；清理异常也拒绝结果。现有 Linux 路径仍使用旧 `subprocess.run`，没有相同进程树保证。
- 新增同一账户数据库内的持久测试风险闸：状态不明及租约过期会阻断后续金标、审核通过和发布；过期任务标记与风险闸同事务提交。任一金标运行期间也暂停新的审核通过和发布，以关闭跨进程超时与写闸之间的间隙。写闸失败时保留 running 记录；重启或其他同库进程仍受阻断。允许退回候选和规则回滚，不提供在线解除风险闸。
- 页面区分测试进行中、暂停及状态未确认；在服务器明确回复可用前禁用新测试与审核通过，允许退回。状态刷新失败时也会禁用当前已显示的审核通过按钮；正常测试结束后在当前页面恢复审核入口。

## 验证

- 此前运行 `python -X utf8 -m pytest -q tests/test_rule_candidate_validation.py tests/test_regression_process.py tests/test_rule_validation_service.py tests/test_rule_release.py tests/test_local_rule_management_api.py tests/test_rule_review_frontend.py`：46 passed in 303.32s，退出码 0。该次通过后还修改了守卫、接口和页面，不能作为最终文件版本的完整专项结果。
- 最后一次守卫及前端修复后：规则管理浏览器风险状态专项 5 passed、8 deselected in 20.21s；服务端风险闸专项 6 passed、15 deselected in 42.44s，退出码均为 0。
- 真实金标超时测试在 Windows 启动合成孙进程，确认超时不通过、延迟写未发生、工作区保留及后续被阻断；持久闸测试含独立新进程、写闸失败和过期任务。
- Ruff 和 `node --check app/static/rules_review.js` 通过。独立审核提出并推动修复了目录过早清理、同库重启阻断、退回入口和异常刷新按钮等问题。

## 未完成及限制

- 同库风险闸不协调独立数据库或独立主机；真实并发双 worker 抢占及强杀服务后的整套恢复尚未端到端验证。解除风险闸必须离线核验进程和保留工作区并制定安全操作流程，当前没有用户界面。
- Job 活动数归零及部分文件释放测试不能证明所有进程资源均已释放；Windows 正常结束后的清理仍不构成通用安全保证。Linux 旧路径的超时和输出管道问题仍待处理。
- 品类金标不等于候选完整回归，尚未产生正式发布所需的真实完整回归证据；当前工作区代码也尚未重新运行全量回归或生产验证，本轮没有部署。
