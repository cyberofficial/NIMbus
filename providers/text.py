"""Shared text extraction utilities."""

from typing import Any


def extract_text_from_content(content: Any) -> str:
    """Extract concatenated text from message content (str or list of content blocks)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if hasattr(block, "text"):
                text = block.text
            elif isinstance(block, dict):
                text = block.get("text", "")
            else:
                text = ""
            if text and isinstance(text, str):
                parts.append(text)
        return "".join(parts)
    return ""


def extract_last_text_content(content: Any) -> str:
    """Extract text from the LAST content block only (for inline command detection).

    Returns the text of the last block if it's a text block, otherwise empty string.
    This avoids matching inline commands in system prompts or earlier context.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list) and content:
        last_block = content[-1]
        if hasattr(last_block, "text"):
            return last_block.text or ""
        elif isinstance(last_block, dict):
            return last_block.get("text", "") or ""
    return ""
