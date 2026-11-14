# 开发后报告：规则测试跨进程验证（Claude Code 批次）

日期：2026-11-10。前序：`docs/handoff-20261110-multiprocess-before.md`（计划）、`docs/handoff-20261110-to-claude-code.md`（总入口）。本批次**仅修改 `tests/test_rule_validation_multiprocess.py`**，并维护本报告。各回合均未提交、未部署；由 Codex 负责运行 Ruff 与测试并独立审核。

## 安全定位（重要声明）

本测试的子进程控制**不是隔离沙箱**，仅适用于**可信本机**环境：
- worker 的 app 来源断言是**事后检测**，不能阻止导入阶段恶意代码已经执行；
- `-S` 只是关闭 site/sitecustomize/.pth 的自动执行入口，**不等于沙箱**；依赖目录机制的前提是本机 Python 路径配置可信，且推导目录中**同目录内其他模块未必运行过**——子进程执行面并不严格限于父进程已执行面；
- `communicate` 假设子进程输出可信且有限，**没有内存大小上限**，恶意或失控子进程仍可耗尽内存；
- 密钥面已收敛：子进程环境只含 Windows/Python 必需白名单项，不继承任何原始环境变量或用户 PYTHONPATH。

## 测试覆盖范围（明确边界）

本测试覆盖且仅覆盖三件事：**运行中互斥**（second worker 在 first 持有任务期间被拒绝）、**模拟时钟过期**（仅推进本进程内 `module.time.time` 601 秒，使 first 的租约视为过期）、**持久阻断**（过期后落闸 `blocked=1/expired_job`、任务 `abandoned`、结果计数为 0、后续任务被拒）。**不构成**真正同时首发竞争的证明，也未验证真实金标进程树：worker 全部经 monkeypatch 替换为假 runner，时序由文件标记串行编排；租约过期依赖第三 worker 的时钟推进，而非真实墙钟流逝。

## 实测结果（Codex 独立运行，Claude Code 转录）

- **第三轮版本**：项目内 `1 passed in 28.13s`；项目外 `1 passed in 36.00s`；Ruff 通过。
- **第四轮版本**：项目内 `1 passed in 19.83s`；Ruff 通过。
- **第五轮版本（测试逻辑的当前版本）**：pytest 项目内 `1 passed in 28.66s`；Ruff 通过；`git diff --check` 退出码 0。**未跑项目外目录、未跑全量回归，不能声称完整回归**。
- **第六轮（当前文件状态）**：仅修正两处注释措辞，测试逻辑与第五轮版本零差异；注释修改后未重新运行 Ruff/pytest（本回合不运行命令），如需完全对应请 Codex 复核。

## 变更记录

### 第一轮：初版修复

1. **Ruff F811**：导入别名化并保留 `# noqa: F401`（fixture 需出现在本模块命名空间才会注册）。
2. **subprocess 失败路径清理**：新增 `_stop()` 兜底；worker 写参数失败时回收进程再上抛；`_result` 超时先终止再上抛；四个 worker 均有 `finally: _stop(...)`（原版只清理 `first`）。

### 第二轮：按独立审核修复

1. **fixture 解析失败修复**。实测本仓 pytest 将测试模块导入的 fixture 按**导入别名**注册。若测试参数直接改名 `setup_fixture` 又会与导入同名，重新触发 F811。最终方案：**wrapper fixture 转发**——导入保持 `from tests.test_rule_validation_service import setup as setup_fixture  # noqa: F401`；本模块定义 `@pytest.fixture def setup(request): return request.getfixturevalue("setup_fixture")`；测试参数保持 `setup`。全程无任何参数/定义与导入同名，且不依赖模块收集顺序。
2. **子进程环境变量白名单**。不再 `os.environ.copy()`，白名单继承 Windows/Python 必需项，显式设 `OWNED_LLM_EXECUTION=0` 关闭 owned LLM 执行通道。
3. **管道阻塞修复**。`_result` 改用 `communicate(timeout=40)` 并发读空管道，避免子进程输出写满缓冲导致 `wait` 永不返回。

### 第三轮：依赖路径 / stdin / stderr 诊断

1. **依赖路径显式重建**。白名单排除 `PYTHONPATH` 后独立运行报 `ModuleNotFoundError: No module named yaml`（本机依赖目录来自父进程 Python 路径配置）。改为从当前解释器显式构造导入路径；`STORE_DB_PATH` 钉死为 fixture 的临时 SQLite。
2. **stdin 交给 communicate**。参数经 `communicate(input=payload)` 写入；仅 `first` 经 `_start()` 手动写入。
3. **stderr 诊断脱敏**。仅进程退出后读取，token→`***`、截尾 2000 字符。

### 第四轮：cwd / 依赖收窄 / 统一脱敏 / 同名 app 守卫

1. **子进程 cwd 钉在项目根**，外部同名 `app` 包无法借 `sys.path[0]` 抢先。
2. **依赖目录收窄**（替代完整 `sys.path` 拷贝）：项目根 + site-packages + 从父进程已加载模块文件位置反推的外部目录。
3. **`_result` stderr 脱敏**与 first 口径一致。
4. **同名 app 干扰的可检查实现**：worker 断言实际加载的 `app` 包解析出的项目根等于 args 传入的 `project_root`。

### 第五轮：按独立审核修复（-S 启动 / 导入面代码内构造 / message 脱敏 / 清理如实上报）

1. **`-S` 启动 + 导入面代码内构造，替代 PYTHONPATH 环境变量**。第四轮仍有缺口：用户 PYTHONPATH 目录中的 sitecustomize 会在子进程启动阶段执行，app 断言无法阻止。修复：
   - `Popen` 加 `-S`：site/sitecustomize/.pth **一律不执行**（仅减少自动执行入口，不构成沙箱）；
   - 不再设置 `PYTHONPATH` 环境变量；依赖目录经 args（stdin JSON）传入 worker，worker **先读参数再导入**，用代码精确构造导入面：`sys.path[0:0] = [project_root]`（app 来源确定），`sys.path.extend(dependency_dirs)` 追加在 stdlib **之后**（依赖目录无法遮蔽标准库）；
   - `dependency_dirs` 两部分：全局 `site.getsitepackages()` 中实际存在的目录（解释器自身安装目录；-S 后需显式带上）；`_extra_dependency_dirs()` 从父进程已加载模块文件位置推导的外部目录（位于项目根与解释器前缀之外）。用户 site-packages 不整体枚举，实际用到的用户目录经推导自然包含。
2. **worker JSON `message` 脱敏**。`_result` 对返回 JSON 的 `message`（自由文本，底层异常可能带上 token）在展示前同样 `_redact`（token→`***`、截尾 2000 字符）。
3. **`_stop` 清理如实上报**。原版 kill 在 try/finally 外，kill 抛错会跳过 wait 和管道关闭。修复：kill/wait/close 每步独立 try，失败不中断后续清理，全部失败项收集后抛 `RuntimeError("child cleanup incomplete: ...")`——若测试异常正在传播，原异常以 `__context__` 链保留，不会被覆盖。**不声称绝对回收**：控制失败时进程可能仍残留，由失败输出如实体现。

### 第六轮：最终审核修订（仅注释与文档，逻辑零改动）

1. **修正 `_extra_dependency_dirs` 的过度声明**：原文称推导目录为"必需依赖""不引入新执行面"。事实上推导只取**承载了已加载模块**的目录，同一目录内的其他模块未必运行过，子进程可能执行父进程未执行过的代码。注释改为"启发式推导（并非必需依赖白名单）"并写明该边界；`_dependency_dirs` 的引用措辞同步修正。
2. **文档明确**：`-S` 不等于沙箱；`communicate` 无内存大小上限（前提是输出可信且有限）；特殊虚拟环境、经 .pth 安装的依赖、Linux 路径均未验收。
3. 本轮不运行命令；第五轮 Codex 验证结果如实记入上文实测结果一节，且注明不能声称完整回归。

## 进程回收路径清单（当前状态）

- `_worker`：Popen 后不触碰 stdin，无失败泄漏点；参数写入移交 `_start` / `_result`。
- `_start`（仅 first）：stdin 写入失败 → `_stop` 后上抛。
- `_result`：communicate 超时 → kill → 排空回收 → 上抛；正常路径读完输出即脱敏断言。
- 测试体：`first`/`second`/`third`/`fourth` 四个 worker 均有 `finally: _stop(...)`；`_stop` 各步失败均如实抛错（见第五轮 3）。
- 进程树边界：worker 用假 runner、无孙进程，`Popen.kill()`（Windows TerminateProcess）足够；若未来接真实金标子进程树，需改用进程树收尾（参见 `tests/test_rule_candidate_validation.py` 及其 PID 复用风险注记）。

## 尚未验证事项

1. **第六轮注释修改后未重新运行 Ruff/pytest**：测试逻辑与已验证的第五轮版本零差异，但严格对应请以 Codex 复核为准。
2. **依赖推导的边界**：启发式按"已加载模块所在目录"取目录，非必需依赖白名单；父进程未加载的延迟导入依赖会漏目录，失败经脱敏 stderr 诊断可见。
3. **环境覆盖缺口**：特殊虚拟环境（.venv、conda）、经 .pth 机制安装的依赖、Linux 平台路径均未验收；`-S` 下依赖 .pth 的安装方式会失效（脱敏 stderr 诊断可见）。
4. **资源边界**：`communicate` 无内存上限，以"子进程输出可信且有限"为前提。
5. **脱敏完备性**：仅覆盖 stderr 与返回 JSON 的 `message` 字段（后者为 worker 产出的唯一自由文本字段）；`outcome`/`type` 为受控枚举值。
6. **communicate 超时分支未实测**；`_stop` 的 RuntimeError 路径未实测（需注入控制失败才能触发，未做故障注入）。
7. 时间推进、租约过期与风险闸行为本身属 `app/services/rule_validation_service.py` 既有专项覆盖，本批次未触碰、未复测；真实双 worker 同时首发竞争、服务强杀、Linux 路径仍待总入口报告所列验收项完成。
8. 遗留进程检查未在每轮执行：第五轮 Codex 已实际运行通过，但"无 `python -c` 残留"的独立复核未记录；本文件失败路径均有 finally 兜底，但不覆盖 pytest 进程本身被强杀的场景。
