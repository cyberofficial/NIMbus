"""Request utility functions for API route handlers.

Contains token counting for API requests.
"""

import json

import tiktoken
from loguru import logger

from providers.message_converter import get_block_attr

ENCODER = tiktoken.get_encoding("cl100k_base")

# Special tokens like <|endoftext|> appear in Claude Code's system prompts
# (DeepSeek uses them in chat templates). Allow them through token counting.
_ALLOWED_SPECIAL: set[str] = {"<|endoftext|>", "<|fim_prefix|>", "<|fim_middle|>",
                               "<|fim_suffix|>", "<|endofprompt|>"}


def _count_tokens(text: str) -> int:
    """Encode text to token count, allowing known special tokens through."""
    try:
        return len(ENCODER.encode(text, allowed_special=_ALLOWED_SPECIAL))
    except Exception:
        return len(ENCODER.encode(text, disallowed_special=()))

__all__ = ["get_token_count"]


def get_token_count(
    messages: list,
    system: str | list | None = None,
    tools: list | None = None,
) -> int:
    """Estimate token count for a request.

    Uses tiktoken cl100k_base encoding to estimate token usage.
    Includes system prompt, messages, tools, and per-message overhead.
    """
    total_tokens = 0

    if system:
        if isinstance(system, str):
            total_tokens += _count_tokens(system)
        elif isinstance(system, list):
            for block in system:
                text = get_block_attr(block, "text", "")
                if text:
                    total_tokens += _count_tokens(str(text))
        total_tokens += 4  # System block formatting overhead

    for msg in messages:
        if isinstance(msg.content, str):
            total_tokens += _count_tokens(msg.content)
        elif isinstance(msg.content, list):
            for block in msg.content:
                b_type = get_block_attr(block, "type") or None

                if b_type == "text":
                    text = get_block_attr(block, "text", "")
                    total_tokens += _count_tokens(str(text))
                elif b_type == "thinking":
                    thinking = get_block_attr(block, "thinking", "")
                    total_tokens += _count_tokens(str(thinking))
                elif b_type == "tool_use":
                    name = get_block_attr(block, "name", "")
                    inp = get_block_attr(block, "input", {})
                    block_id = get_block_attr(block, "id", "")
                    total_tokens += _count_tokens(str(name))
                    total_tokens += _count_tokens(json.dumps(inp))
                    total_tokens += _count_tokens(str(block_id))
                    total_tokens += 15
                elif b_type == "image":
                    source = get_block_attr(block, "source")
                    if isinstance(source, dict):
                        data = source.get("data") or source.get("base64") or ""
                        if data:
                            total_tokens += max(85, len(data) // 3000)
                        else:
                            total_tokens += 765
                    else:
                        total_tokens += 765
                elif b_type == "tool_result":
                    content = get_block_attr(block, "content", "")
                    tool_use_id = get_block_attr(block, "tool_use_id", "")
                    if isinstance(content, str):
                        total_tokens += _count_tokens(content)
                    else:
                        total_tokens += _count_tokens(json.dumps(content))
                    total_tokens += _count_tokens(str(tool_use_id))
                    total_tokens += 8
                else:
                    logger.debug(
                        "Unexpected block type %r, falling back to json/str encoding",
                        b_type,
                    )
                    try:
                        total_tokens += _count_tokens(json.dumps(block))
                    except (TypeError, ValueError):
                        total_tokens += _count_tokens(str(block))

    if tools:
        for tool in tools:
            tool_str = (
                tool.name + (tool.description or "") + json.dumps(tool.input_schema)
            )
            total_tokens += _count_tokens(tool_str)

    total_tokens += len(messages) * 4
    if tools:
        total_tokens += len(tools) * 5

    return max(1, total_tokens)
