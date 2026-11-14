# Claude Code 交接：候选回归隔离工作区阶段结果

## 已实现

- 新增内部临时工作区构造器，复制完整回归指纹覆盖的普通输入文件，排除缓存与环境密钥文件；复制前后比较源和副本指纹，校验并只在副本中替换目标候选规则。文件数及字节量有上限，散列和复制按块处理，超限、源漂移和校验失败拒绝使用。
- 独立审核提出的“大小检查晚于全量读文件”已修复；发布端指纹计算也增加相同大小及文件数上限。
- 在真实 Python 子进程中确认模块、规则目录和规则摘要均来自副本，且停用条目不再出现。额外在副本内启动真实 pytest：同一金标用原规则通过，换成故意停用的候选后因指定检查项缺失而失败；原项目规则未改变。

## 验证

- 隔离临时数据库、禁用真实模型密钥：`python -X utf8 -m pytest -q tests/test_candidate_workspace.py tests/test_rule_release.py tests/test_local_rule_release_api.py`：11 passed in 31.77s。负对照中的子 pytest 的预期结果是失败，被外层测试验证。
- `python -m ruff check app/services/candidate_workspace.py app/services/rule_release.py tests/test_candidate_workspace.py tests/test_rule_release.py`：通过。

## 未完成与边界

- 本轮只构造和验证候选工作区，不执行完整候选回归，不写发布证据，也没有开放正式发布。此前 1863 passed、34 skipped 的全量基线先于本轮代码变更，不能作为本轮版本的全量回归。
- 副本不是安全沙箱；目前依赖来自本机 Python 环境，未验证依赖不可变或网络隔离。测试包含本机已有的 `fixtures-real` 输入，具体数据使用范围和保留策略仍需确认。文件检查到打开之间仍有链接替换竞态，不应在不可信本机写入者环境下启用。
- 尚未实现受控执行全套候选测试、独立服务/浏览器子进程的候选证明、超时后整个进程树清理、跳过策略和结果持久化。正式发布、生产部署与真实模型验收均未完成。
