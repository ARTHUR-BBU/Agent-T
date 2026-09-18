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
from typing import Any, Callable


def source_version(fn: Callable[..., str], *extras: Any) -> str:
    """以 prompt 构建函数源码 + 关联素材的 sha256 前 12 位为版本标识。

    extras 可传：模块级常量字符串（DIRECTION_LINES 等——渲染内容变了
    版本必须变）、其他 prompt 函数（build_retry_system_prompt 等——
    retry 指令变化同样要改版本，Codex P2：失败调用的复现恰恰依赖它）。
    """
    try:
        parts = [inspect.getsource(fn)]
        for e in extras:
            parts.append(inspect.getsource(e) if callable(e) else str(e))
        blob = "\x1f".join(parts)
    except (OSError, TypeError):
        return "v0-unknown"
    digest = hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]
    return f"sha-{digest}"
