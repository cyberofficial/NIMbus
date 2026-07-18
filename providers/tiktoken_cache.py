"""Shared tiktoken encoder cache to avoid redundant loads."""

import os
import tiktoken
from typing import Optional

_TIKTOKEN_CACHE: dict[str, "tiktoken.Encoding"] = {}


def get_encoder(encoding: str = "cl100k_base") -> "tiktoken.Encoding":
    """Get cached tiktoken encoder. Uses TIKTOKEN_CACHE_DIR if set."""
    global _TIKTOKEN_CACHE

    if encoding not in _TIKTOKEN_CACHE:
        _TIKTOKEN_CACHE[encoding] = tiktoken.get_encoding(encoding)
    return _TIKTOKEN_CACHE[encoding]