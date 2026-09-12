"""全局测试夹具。

预审默认关闭：现有全套测试（金标/回放/E2E）的上传路径不允许依赖真实 LLM
分类（CI 带真实 Key 时会把测试结果变成非确定性）。预审自己的测试用
monkeypatch.setenv("PRECHECK_ENABLED", "true") 显式打开。

限频默认关闭：全套测试共享进程内限频器单例，TestClient 的 IP 恒为
"testclient"，一次 pytest 的 upload 总数远超默认 10 次/分钟——不关必挂
全部 API 测试。限频自己的测试用 monkeypatch.setenv 显式设置限额，并在
teardown 清桶防止污染后续测试文件（阶段 0.5）。
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _precheck_off(monkeypatch):
    monkeypatch.setenv("PRECHECK_ENABLED", "false")


@pytest.fixture(autouse=True)
def _quality_off(monkeypatch):
    """质量层默认关闭（生产默认开）：与 _precheck_off 同构——质量层自己的
    测试用 monkeypatch.setenv("QUALITY_ENABLED", "true") 显式打开，
    其余全套测试的上传路径不依赖 LLM 观察层（确定性红线）。"""
    monkeypatch.setenv("QUALITY_ENABLED", "false")


@pytest.fixture(autouse=True)
def _rate_limit_off(monkeypatch):
    # 时序提醒（小智娘门禁 P3-5）：本 fixture 是 function 级，module/session 级
    # fixture 的 setup 发生在它之前，若未来有人在模块级 fixture 里打 API 且
    # 未显式设限频 env，会在默认限额下踩 429——届时请在那个 fixture 的 env
    # 里显式关闭（参照 test_frontend_e2e.py 的做法）
    monkeypatch.setenv("RATE_LIMIT_UPLOAD_PER_MINUTE", "0")
    monkeypatch.setenv("RATE_LIMIT_ASK_PER_MINUTE", "0")


@pytest.fixture(autouse=True)
def _rate_limit_reset():
    yield
    from app.services import rate_limit

    rate_limit.reset_for_tests()
