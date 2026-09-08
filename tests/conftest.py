"""全局测试夹具。

预审默认关闭：现有全套测试（金标/回放/E2E）的上传路径不允许依赖真实 LLM
分类（CI 带真实 Key 时会把测试结果变成非确定性）。预审自己的测试用
monkeypatch.setenv("PRECHECK_ENABLED", "true") 显式打开。
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _precheck_off(monkeypatch):
    monkeypatch.setenv("PRECHECK_ENABLED", "false")
