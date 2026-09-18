"""Prompt 版本机制（宪法 P0-D4）。

方案（LLM 规范 22 节允许 content-hash）：以 prompt 构建函数的**源码内容哈希**
为版本号——改 prompt 即改版本，无需手工维护 semver（人工版本号会忘改，
哈希不会）。落账处在 LLMCallRecord.prompt_version（P0-D1）。

用法：各 prompt 模块在定义 build_system_prompt 后声明：
    PROMPT_VERSION = source_version(build_system_prompt)
"""
from __future__ import annotations

import hashlib
import inspect
from typing import Callable


def source_version(fn: Callable[..., str]) -> str:
    """以函数源码 sha256 前 12 位作为 prompt 版本标识。

    注意：哈希覆盖函数源码及其闭包引用的模块级常量不会自动纳入——
    prompt 模板若提取为模块级常量，应把常量也传入（source_version(fn, CONST)）。
    """
    try:
        src = inspect.getsource(fn)
    except (OSError, TypeError):
        return "v0-unknown"
    digest = hashlib.sha256(src.encode("utf-8")).hexdigest()[:12]
    return f"sha-{digest}"
