"""Backward-compatible re-exports; prefer app.services.llm_ask."""
from __future__ import annotations

from app.services.llm_ask import (  # noqa: F401
    DEFAULT_GROK_MODEL as DEFAULT_MODEL,
    EnvAskAuth as EnvGrokAuth,
    AskAuthProvider as GrokAuthProvider,
    OUTPUT_FIELDS,
    XAI_CHAT_URL,
    ask_about_item,
    get_api_key,
)

__all__ = [
    "DEFAULT_MODEL",
    "EnvGrokAuth",
    "GrokAuthProvider",
    "OUTPUT_FIELDS",
    "XAI_CHAT_URL",
    "ask_about_item",
    "get_api_key",
]
