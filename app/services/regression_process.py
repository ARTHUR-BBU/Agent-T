"""Windows process-tree control for internal tests; not a security sandbox."""
from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence


@dataclass(frozen=True)
class ProcessTreeResult:
    exit_code: int | None
    timed_out: bool
    interrupted_descendants: bool


def run_process_tree(command: Sequence[str], *, cwd: Path, env: Mapping[str, str],
                     timeout_seconds: float) -> ProcessTreeResult:
    """Run an internal command only after placing its launcher in a Windows job.

    Output is discarded: callers must use their own bounded structured result file.
    The timeout starts after assignment. A returned result is not a test verdict
    or proof that every process-owned resource has been released.
    """
    if os.name != "nt":
        raise RuntimeError("当前平台尚不支持受控回归进程树")
    if (not command or any(not isinstance(item, str) or not item for item in command)
            or isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float))
            or not 0 < timeout_seconds <= 3600):
        raise ValueError("回归进程参数无效")
    return _run_windows(command, cwd=cwd, env=env, timeout_seconds=timeout_seconds)


def _run_windows(command: Sequence[str], *, cwd: Path, env: Mapping[str, str],
                 timeout_seconds: float) -> ProcessTreeResult:
    import ctypes
    from ctypes import wintypes

    class BasicLimits(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_uint64) for name in ("ReadOperationCount", "WriteOperationCount",
                    "OtherOperationCount", "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", BasicLimits), ("IoInfo", IoCounters),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    class Accounting(ctypes.Structure):
        _fields_ = [("TotalUserTime", ctypes.c_int64), ("TotalKernelTime", ctypes.c_int64),
                    ("ThisPeriodTotalUserTime", ctypes.c_int64), ("ThisPeriodTotalKernelTime", ctypes.c_int64),
                    ("TotalPageFaultCount", wintypes.DWORD), ("TotalProcesses", wintypes.DWORD),
                    ("ActiveProcesses", wintypes.DWORD), ("TotalTerminatedProcesses", wintypes.DWORD)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
    kernel.SetInformationJobObject.restype = wintypes.BOOL
    kernel.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    kernel.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
    kernel.TerminateJobObject.restype = wintypes.BOOL
    kernel.QueryInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                                wintypes.DWORD, ctypes.c_void_p)
    kernel.QueryInformationJobObject.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.CloseHandle.restype = wintypes.BOOL

    job = kernel.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    process = None
    assigned = False
    reached_wait = False
    interrupted_descendants = False
    try:
        limits = ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = 0x00002000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            raise ctypes.WinError(ctypes.get_last_error())
        # -I -S prevents site hooks before the launcher waits for assignment.
        bootstrap = ("import subprocess,sys; "
                     "sys.exit(subprocess.run(sys.argv[1:]).returncode if sys.stdin.buffer.read(1)==b'1' else 125)")
        process = subprocess.Popen([sys.executable, "-I", "-S", "-c", bootstrap, *command], cwd=cwd,
                                   env=dict(env), stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
        if not kernel.AssignProcessToJobObject(job, wintypes.HANDLE(int(process._handle))):
            raise ctypes.WinError(ctypes.get_last_error())
        assigned = True
        assert process.stdin is not None
        process.stdin.write(b"1")
        process.stdin.close()
        try:
            exit_code = process.wait(timeout=timeout_seconds)
            timed_out = False
            # ActiveProcesses may still include the exited launcher while this
            # process holds its handle; release it before testing for survivors.
            process._handle.Close()
        except subprocess.TimeoutExpired:
            exit_code, timed_out = None, True
        reached_wait = True
    finally:
        try:
            if assigned:
                if reached_wait and not timed_out:
                    settle_deadline = time.monotonic() + 1
                    while True:
                        before = Accounting()
                        if not kernel.QueryInformationJobObject(job, 1, ctypes.byref(before), ctypes.sizeof(before), None):
                            raise ctypes.WinError(ctypes.get_last_error())
                        if before.ActiveProcesses == 0 or time.monotonic() >= settle_deadline:
                            break
                        time.sleep(.05)
                    interrupted_descendants = before.ActiveProcesses > 0
                if not kernel.TerminateJobObject(job, 1):
                    raise ctypes.WinError(ctypes.get_last_error())
                deadline = time.monotonic() + 10
                while True:
                    info = Accounting()
                    if not kernel.QueryInformationJobObject(job, 1, ctypes.byref(info), ctypes.sizeof(info), None):
                        raise ctypes.WinError(ctypes.get_last_error())
                    if info.ActiveProcesses == 0:
                        break
                    if time.monotonic() >= deadline:
                        raise RuntimeError("回归进程树未能及时关闭")
                    time.sleep(.05)
            elif process is not None and process.poll() is None:
                process.kill()
        finally:
            try:
                if process is not None:
                    if process.stdin and not process.stdin.closed:
                        process.stdin.close()
                    process.wait(timeout=10)
            finally:
                if not kernel.CloseHandle(job):
                    raise ctypes.WinError(ctypes.get_last_error())
    return ProcessTreeResult(exit_code=exit_code, timed_out=timed_out,
                             interrupted_descendants=interrupted_descendants)
