"""Actual separate-process checks for the validation job guard."""
import json
import os
import pathlib
import site
import subprocess
import sys
import time

import pytest

# 本仓 pytest 把测试模块导入的 fixture 按导入别名注册（实测 available fixture 是 setup_fixture）。
# 若测试参数直接用别名会与导入同名（Ruff F811），故下方用 wrapper fixture 转发。
from tests.test_rule_validation_service import setup as setup_fixture  # noqa: F401

# 项目根由本文件位置推导，不依赖 pytest 的启动目录
_PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]


_WORKER = r'''
import hashlib, json, pathlib, sys, time

args = json.loads(sys.stdin.readline())
# 以 -S 启动：site/sitecustomize/.pth 一律不执行，导入面完全由下面两行决定。
# 项目根插到最前保证 app 来自预期位置；依赖目录追加在 stdlib 之后，无法遮蔽标准库。
sys.path[0:0] = [args["project_root"]]
sys.path.extend(args["dependency_dirs"])

from app.services.account_store import AccountStore
from app.services.rule_candidate_validation import GoldCheckResult
from app.services.rule_validation_service import RuleValidationService
from app.services import rule_validation_service as module
import app as app_package

# 防"同名 app 抢先导入"：断言实际加载的 app 包来自预期项目根，被劫持时大声失败。
# 注意这只是事后检测，不能阻止导入阶段恶意代码已经执行——本测试仅限可信本机。
_origin = getattr(app_package, "__file__", None) or next(iter(getattr(app_package, "__path__", ())), "")
_loaded_root = pathlib.Path(_origin).resolve().parent.parent
assert _loaded_root == pathlib.Path(args["project_root"]).resolve(), f"imported wrong app package: {_loaded_root}"

service = RuleValidationService(AccountStore(pathlib.Path(args["database"])))
if args["advance"]:
    original_time = time.time
    module.time.time = lambda: original_time() + 601

def run(snapshot):
    pathlib.Path(args["entered"]).write_text("entered", encoding="utf-8")
    if args["wait"]:
        while not pathlib.Path(args["release"]).exists():
            time.sleep(0.05)
    return GoldCheckResult(category=snapshot.category,
        candidate_sha256=hashlib.sha256(snapshot.config_bytes).hexdigest(),
        input_manifest_sha256="a" * 64, suite="fake", passed=True,
        passed_tests=1, collected_tests=1, skipped_tests=0, failed_test_ids=(),
        exit_code=0, output_sha256="b" * 64)

module.run_gold_checks = run
try:
    service.validate(args["token"], args["candidate"])
    print(json.dumps({"outcome": "completed"}), flush=True)
except Exception as exc:
    print(json.dumps({"outcome": "rejected", "type": type(exc).__name__, "message": str(exc)}, ensure_ascii=True), flush=True)
'''


# 子进程环境变量白名单：只保留 Windows 与 Python 正常运行必需项，
# 不继承本机 .env / 密钥等任何原始环境变量；导入路径不经环境变量传递（见 _WORKER）。
_CHILD_ENV_KEYS = frozenset({
    "COMSPEC", "HOMEDRIVE", "HOMEPATH", "LOCALAPPDATA", "PATH", "PATHEXT",
    "PROCESSOR_ARCHITECTURE", "SYSTEMDRIVE", "SYSTEMROOT", "TEMP", "TMP",
    "USERPROFILE", "WINDIR",
})


def _under(path, base):
    base = os.path.abspath(base)
    try:
        return os.path.commonpath([path, base]) == base
    except ValueError:
        # Windows 跨盘符等场景 commonpath 会抛 ValueError
        return False


def _import_root_of(path):
    """从一个模块文件位置上溯到真正的导入条目（越过带 __init__.py 的包目录）。"""
    entry = os.path.dirname(path)
    while os.path.isfile(os.path.join(entry, "__init__.py")):
        parent = os.path.dirname(entry)
        if parent == entry:
            break
        entry = parent
    return entry


def _extra_dependency_dirs():
    """外部依赖目录的启发式推导（并非"必需依赖"白名单）。

    本机为系统 Python（无 .venv），部分依赖位于解释器常规安装目录之外（实测不传即缺
    yaml）。从父进程已加载模块的文件位置反推候选目录，只保留位于项目根与解释器前缀
    之外、承载了已加载模块的导入目录。注意：同一目录内的其他模块未必运行过，子进程
    可能执行父进程未执行过的代码；此处只读路径元数据，不读取任何密钥。
    """
    entries = []
    for module in list(sys.modules.values()):
        origin = getattr(module, "__file__", None)
        if not origin:
            continue
        path = os.path.abspath(origin)
        if (_under(path, _PROJECT_ROOT) or _under(path, sys.prefix)
                or _under(path, sys.base_prefix)):
            continue
        entry = _import_root_of(path)
        if entry and os.path.isdir(entry) and entry not in entries:
            entries.append(entry)
    return entries


def _dependency_dirs():
    """子进程导入路径的依赖部分（追加在 stdlib 之后）。

    - 全局 site-packages：解释器自身的可信安装目录；-S 后子进程不会自动挂载，需显式带上。
      用户 site-packages 不整体枚举——实际用到的用户目录会经下方推导自然包含。
    - _extra_dependency_dirs：常规安装目录之外的外部目录（启发式推导，边界见其说明）。
    """
    entries = []
    try:
        for candidate in site.getsitepackages():
            if candidate and os.path.isdir(candidate):
                entries.append(os.path.abspath(candidate))
    except Exception:
        pass
    entries.extend(_extra_dependency_dirs())
    return list(dict.fromkeys(entries))


def _child_env(store_db_path):
    env = {key.upper(): value for key, value in os.environ.items()
           if key.upper() in _CHILD_ENV_KEYS}
    # 钉死独立临时数据库路径（pytest tmp_path 下），杜绝任何默认真实数据库访问
    env["STORE_DB_PATH"] = str(store_db_path)
    # 显式关闭 owned LLM 执行通道，确保 worker 不真实外呼模型
    env["OWNED_LLM_EXECUTION"] = "0"
    return env


@pytest.fixture
def setup(request):
    return request.getfixturevalue("setup_fixture")


def _stop(process):
    """尽力清理子进程：终止、等待、关管道每步都尝试，任何一步失败不中断后续清理。

    不声称绝对回收：kill/wait/close 失败时抛 RuntimeError 如实上报（若测试异常正在
    传播，该异常会以 __context__ 链保留），进程可能仍残留。
    """
    failures = []
    try:
        if process.poll() is None:
            process.kill()
    except OSError as exc:
        failures.append(f"kill: {exc}")
    try:
        process.wait(timeout=10)
    except Exception as exc:
        failures.append(f"wait: {exc}")
    for stream in (process.stdin, process.stdout, process.stderr):
        if stream is None:
            continue
        try:
            stream.close()
        except OSError as exc:
            failures.append(f"close: {exc}")
    if failures:
        raise RuntimeError("child cleanup incomplete: " + "; ".join(failures))


def _redact(text, redactions, limit=2000):
    for secret in redactions:
        if secret:
            text = text.replace(secret, "***")
    return text[-limit:]


def _read_errors(process, *, redactions=()):
    """读取已退出进程的 stderr 用于诊断，先脱敏再截尾，避免泄露 token/密钥。"""
    try:
        text = process.stderr.read() or ""
    except OSError:
        return ""
    return _redact(text, redactions)


def _worker(tmp_path, accounts, token, candidate, *, name, wait=False, advance=False):
    entered = tmp_path / (name + ".entered")
    release = tmp_path / (name + ".release")
    args = {"database": str(accounts.path), "token": token, "candidate": candidate["id"],
            "project_root": str(_PROJECT_ROOT), "dependency_dirs": _dependency_dirs(),
            "entered": str(entered), "release": str(release), "wait": wait, "advance": advance}
    # -S：子进程不执行 site/sitecustomize/.pth；cwd 钉在项目根
    process = subprocess.Popen([sys.executable, "-X", "utf8", "-S", "-c", _WORKER],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, encoding="utf-8", env=_child_env(accounts.path),
                               cwd=str(_PROJECT_ROOT))
    # 参数不再手动写入/关闭 stdin：统一交给 _result 的 communicate(input=...)，
    # 避免 communicate 对已关闭的 stdin 做 flush 出错。first 单独走 _start。
    payload = json.dumps(args) + "\n"
    return process, payload, entered


def _start(process, payload):
    """first worker 不走 _result，需要手动送达参数；失败时回收进程再上抛。"""
    try:
        process.stdin.write(payload)
        process.stdin.close()
    except Exception:
        _stop(process)
        raise


def _result(process, payload, *, redactions=()):
    try:
        # communicate 并发读空 stdout/stderr 且由它负责写 stdin，
        # 避免子进程输出写满管道缓冲卡死、或对已关闭 stdin flush 报错
        output, errors = process.communicate(input=payload, timeout=40)
    except subprocess.TimeoutExpired:
        # 等待超时视为失控：先终止，再排空管道回收，然后上抛
        process.kill()
        try:
            process.communicate(timeout=10)
        except Exception:
            pass
        raise
    # 与 first 的诊断口径一致：stderr 脱敏（token→***）并截尾后再展示
    assert process.returncode == 0, _redact(errors, redactions)
    result = json.loads(output)
    # worker 的异常 message 是自由文本，可能被底层错误带上 token，展示前同样脱敏并限长
    if isinstance(result.get("message"), str):
        result["message"] = _redact(result["message"], redactions)
    return result


def test_separate_workers_and_crash_recovery_fail_closed(setup, tmp_path):
    accounts, _, _, _, token, _, candidate = setup
    first, first_payload, entered = _worker(tmp_path, accounts, token, candidate, name="first", wait=True)
    try:
        _start(first, first_payload)
        deadline = time.monotonic() + 40
        while not entered.exists() and first.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        if not entered.exists():
            # 仅在进程已退出时读 stderr（仍在运行时读会阻塞）；token 已脱敏
            if first.poll() is not None:
                diagnostic = _read_errors(first, redactions=(token,))
                raise AssertionError("first worker did not enter its runner: " + diagnostic)
            raise AssertionError("first worker did not enter its runner before deadline")
        second, second_payload, second_entered = _worker(tmp_path, accounts, token, candidate, name="second")
        try:
            result = _result(second, second_payload, redactions=(token,))
            assert result["outcome"] == "rejected" and "正在测试" in result["message"]
            assert not second_entered.exists()
        finally:
            _stop(second)
    finally:
        _stop(first)

    # The dead worker's lease is made expired using only this third worker's clock.
    third, third_payload, third_entered = _worker(tmp_path, accounts, token, candidate, name="third", advance=True)
    try:
        result = _result(third, third_payload, redactions=(token,))
        assert result["outcome"] == "rejected" and "检查进程" in result["message"]
        assert not third_entered.exists()
    finally:
        _stop(third)
    with accounts._connect() as conn:
        job = conn.execute("SELECT id, status FROM rule_validation_jobs").fetchone()
        control = conn.execute("SELECT blocked, reason, last_job_id FROM rule_validation_control WHERE id=1").fetchone()
        assert conn.execute("SELECT COUNT(*) FROM rule_validation_results").fetchone()[0] == 0
    assert job["status"] == "abandoned"
    assert tuple(control) == (1, "expired_job", job["id"])

    fourth, fourth_payload, fourth_entered = _worker(tmp_path, accounts, token, candidate, name="fourth")
    try:
        result = _result(fourth, fourth_payload, redactions=(token,))
        assert result["outcome"] == "rejected" and "待检查" in result["message"]
        assert not fourth_entered.exists()
    finally:
        _stop(fourth)
