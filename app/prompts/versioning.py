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


def source_version(fn: Callable[..., str], *constants: str) -> str:
    """以 prompt 构建函数源码 + 渲染用常量的 sha256 前 12 位为版本标识。

    Codex P2：只哈希函数源码时，模块级常量（如 DIRECTION_LINES/IRON_RULES）
    的变化不会改变版本——渲染内容变了版本必须变。凡被 f-string 渲染进
    prompt 的模块级常量都应作为附加参数传入。
    """
    try:
        src = inspect.getsource(fn)
        blob = "\x1f".join([src, *constants])
    except (OSError, TypeError):
        return "v0-unknown"
    digest = hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]
    return f"sha-{digest}"
