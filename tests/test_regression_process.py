"""Windows job cleanup checks with synthetic processes and no provider access."""
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from app.services.regression_process import run_process_tree

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows Job Object 专项")


def _env():
    return {key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP") if key in os.environ}


def test_normal_and_failing_exit(tmp_path):
    success = run_process_tree([sys.executable, "-c", "pass"], cwd=tmp_path, env=_env(), timeout_seconds=10)
    failed = run_process_tree([sys.executable, "-c", "raise SystemExit(7)"], cwd=tmp_path, env=_env(), timeout_seconds=10)
    assert (success.exit_code, success.timed_out) == (0, False)
    assert (failed.exit_code, failed.timed_out) == (7, False)
    assert not success.interrupted_descendants and not failed.interrupted_descendants


@pytest.mark.parametrize("parent_wait", [True, False])
def test_timeout_or_parent_exit_stops_descendant(tmp_path, parent_wait):
    ready = tmp_path / "child-started.txt"
    marker = tmp_path / "should-not-appear.txt"
    locked = tmp_path / "locked.txt"
    grandchild = """
import ctypes, pathlib, sys, time
from ctypes import wintypes
k = ctypes.WinDLL('kernel32', use_last_error=True)
k.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
k.CreateFileW.restype = wintypes.HANDLE
handle = k.CreateFileW(sys.argv[3], 0xC0000000, 0, None, 2, 0x80, None)
if handle == ctypes.c_void_p(-1).value: raise ctypes.WinError(ctypes.get_last_error())
pathlib.Path(sys.argv[1]).write_text('ready')
time.sleep(5)
pathlib.Path(sys.argv[2]).write_text('late')
"""
    script = """
import pathlib, subprocess, sys, time
subprocess.Popen([sys.executable, '-c', sys.argv[4], sys.argv[1], sys.argv[2], sys.argv[3]])
deadline = time.monotonic() + 10
while not pathlib.Path(sys.argv[1]).exists():
    if time.monotonic() > deadline: raise RuntimeError('child did not start')
    time.sleep(.05)
if sys.argv[5] == 'wait': time.sleep(30)
"""
    result = run_process_tree([sys.executable, "-c", script, str(ready), str(marker), str(locked), grandchild,
                               "wait" if parent_wait else "exit"], cwd=tmp_path, env=_env(), timeout_seconds=3)
    assert ready.read_text() == "ready"
    if parent_wait:
        assert result.timed_out and result.exit_code is None
    else:
        assert not result.timed_out and result.exit_code == 0
        assert result.interrupted_descendants
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                   wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.CreateFileW(str(locked), 0x80000000, 0, None, 3, 0x80, None)
    assert handle != ctypes.c_void_p(-1).value, "controlled descendant still holds its exclusive file"
    assert kernel.CloseHandle(handle)
    time.sleep(5.5)
    assert not marker.exists()


def test_invalid_timeout_and_command_fail_before_start(tmp_path):
    with pytest.raises(ValueError):
        run_process_tree([], cwd=tmp_path, env=_env(), timeout_seconds=1)
    with pytest.raises(ValueError):
        run_process_tree([sys.executable], cwd=tmp_path, env=_env(), timeout_seconds=0)


def test_large_output_does_not_block_process_tree(tmp_path):
    script = "import sys; sys.stdout.write('x' * 2000000); sys.stderr.write('y' * 2000000)"
    result = run_process_tree([sys.executable, "-c", script], cwd=tmp_path, env=_env(), timeout_seconds=15)
    assert (result.exit_code, result.timed_out) == (0, False)


def test_parent_crash_closes_job_and_stops_descendant(tmp_path):
    root = Path(__file__).resolve().parents[1]
    ready = tmp_path / "ready.txt"
    marker = tmp_path / "late.txt"
    child = ("import pathlib,sys,time; pathlib.Path(sys.argv[1]).write_text('ready'); "
             "time.sleep(5); pathlib.Path(sys.argv[2]).write_text('late')")
    supervisor = ("import os,sys; from pathlib import Path; "
                  "from app.services.regression_process import run_process_tree; "
                  "run_process_tree([sys.executable,'-c',sys.argv[3],sys.argv[1],sys.argv[2]], "
                  "cwd=Path(sys.argv[4]),env=os.environ,timeout_seconds=30)")
    env = {**_env(), "PYTHONPATH": str(root)}
    proc = subprocess.Popen([sys.executable, "-c", supervisor, str(ready), str(marker), child, str(tmp_path)],
                            cwd=root, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 15
        while not ready.exists():
            if proc.poll() is not None or time.monotonic() >= deadline:
                pytest.fail("supervisor did not start the controlled process")
            time.sleep(.05)
        proc.kill()
        proc.wait(timeout=10)
        time.sleep(5.5)
        assert not marker.exists()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
