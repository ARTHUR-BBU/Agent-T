"""外部需求（用户 2026-09-15）：支持 .doc 结尾——catdoc 兜底提取。

旧行为：.doc（OLE2）直接友好拒绝。新行为：服务器镜像装 catdoc，
.doc 分支优先走 catdoc -d utf-8 提取；不可用（未装/失败/超时）保持
友好指路不变（fail-closed：绝不产出半吊子文本）。
"""
from __future__ import annotations

import pytest

from app.services.extract import ExtractionError, extract_text

_OLE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64  # OLE2 魔数 + 填充


def test_doc_without_catdoc_friendly_reject(monkeypatch):
    """无 catdoc（本地/CI 常态）：保持既有友好指路，不炸不吐乱码。"""
    import shutil

    monkeypatch.setattr(shutil, "which", lambda _: None)
    with pytest.raises(ExtractionError) as exc_info:
        extract_text("old.doc", _OLE)
    assert "另存为" in str(exc_info.value)


def test_doc_with_catdoc_extracts(monkeypatch):
    """catdoc 可用且解析成功：文本提取（extract_text 返回 str）。"""
    import shutil
    import subprocess

    class _Proc:
        returncode = 0
        stdout = "第一条 租赁标的\n第二条 租金与支付".encode("utf-8")
        stderr = b""

    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/catdoc")
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _Proc())
    text = extract_text("old.doc", _OLE)
    assert "租赁标的" in text


def test_doc_catdoc_failure_still_rejects(monkeypatch):
    """catdoc 在但解析失败（rc≠0）：保持友好指路（fail-closed）。"""
    import shutil
    import subprocess

    class _Proc:
        returncode = 1
        stdout = b""
        stderr = b"catdoc: not an OLE file"

    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/catdoc")
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _Proc())
    with pytest.raises(ExtractionError) as exc_info:
        extract_text("old.doc", _OLE)
    assert "另存为" in str(exc_info.value)


def test_doc_catdoc_garbage_output_rejected(monkeypatch):
    """catdoc 产出乱码（控制字符超阈值）：按 fail-closed 拒绝，不进审查。"""
    import shutil
    import subprocess

    class _Proc:
        returncode = 0
        stdout = bytes(range(0, 32)) * 40  # 控制字符占比 100%
        stderr = b""

    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/catdoc")
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _Proc())
    with pytest.raises(ExtractionError):
        extract_text("old.doc", _OLE)


@pytest.mark.skipif(
    __import__("shutil").which("catdoc") is None,
    reason="本机未装 catdoc（服务器镜像已装）；集成用例仅在有 catdoc 的环境跑",
)
def test_doc_catdoc_real_integration():
    """真 catdoc 集成冒烟：手工构造的极简 OLE 容器至少不产出半吊子成功。"""
    with pytest.raises(ExtractionError):
        # 魔数正确但内容非真 .doc：catdoc 应失败 → 友好指路
        extract_text("old.doc", _OLE)
