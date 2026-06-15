"""Model swapper message parser utilities."""

import re

from providers.text import extract_text_from_content

MODELSWAP_PATTERN = re.compile(r"<modelswap:\s*([^>]+)>", re.IGNORECASE)
MODELSWAP_CLEAR_PATTERN = re.compile(r"<modelswap:\s*clear\s*>", re.IGNORECASE)


def extract_modelswap_tag(text: str) -> str | None:
    """
    Extract model name from <modelswap:model-name> tag.

    Args:
        text: Message content to search

    Returns:
        Model name if found, None otherwise
    """
    match = MODELSWAP_PATTERN.search(text)
    if match:
        return match.group(1).strip()
    return None


def is_modelswap_clear_tag(text: str) -> bool:
    """Check if message contains <modelswap:clear> tag."""
    return bool(MODELSWAP_CLEAR_PATTERN.search(text))


def is_modelswap_message(message: dict) -> bool:
    """
    Check if a message is a modelswap command.

    Args:
        message: Anthropic-format message dict with 'role' and 'content'

    Returns:
        True if user message contains modelswap tag
    """
    if message.get("role") != "user":
        return False

    content = message.get("content", "")
    text = extract_text_from_content(content)
    return extract_modelswap_tag(text) is not None or is_modelswap_clear_tag(text)


def get_modelswap_model(message: dict) -> str | None:
    """
    Get model name from modelswap message, or None if not a swap message.
    Returns special string 'CLEAR' for clear command.
    """
    if message.get("role") != "user":
        return None

    content = message.get("content", "")
    text = extract_text_from_content(content)

    if is_modelswap_clear_tag(text):
        return "CLEAR"

    return extract_modelswap_tag(text)
