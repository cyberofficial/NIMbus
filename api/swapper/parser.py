"""Model swapper and NIM server type parser utilities."""

import re

from providers.text import extract_text_from_content

MODELSWAP_PATTERN = re.compile(r"<modelswap:\s*([^>]+)>", re.IGNORECASE)
MODELSWAP_CLEAR_PATTERN = re.compile(r"<modelswap:\s*clear\s*>", re.IGNORECASE)

NIMSERVER_PATTERN = re.compile(
    r"<nimserver:\s*(stream|buffer)\s*>", re.IGNORECASE
)
NIMSERVER_CLEAR_PATTERN = re.compile(r"<nimserver:\s*clear\s*>", re.IGNORECASE)

# Match <nimrpm:reset> ONLY when it's the ENTIRE message (after optional leading/trailing whitespace)
# This avoids false positives from embedded content in file outputs, system prompts, docs, etc.
NIMRPM_RESET_PATTERN = re.compile(r"^[ \t]*<nimrpm:\s*reset\s*>[ \t]*$", re.IGNORECASE)


def is_nimrpm_reset_tag(text: str) -> bool:
    """Check if message IS exactly the <nimrpm:reset> tag (plus optional whitespace).

    This resets the adaptive rate limiting backoff state,
    restoring original RPM and clearing any hold delay.

    Only matches when the tag is the complete message (after optional
    leading/trailing whitespace) to avoid false positives from embedded
    content like file outputs or documentation snippets.
    """
    return bool(NIMRPM_RESET_PATTERN.search(text))


# Match <nimhelp> ONLY when the message is the literal tag, nothing else — no
# surrounding whitespace, no prose. Consistent with NIMRPM_RESET_PATTERN (which
# tolerates outer whitespace) to avoid false negatives from trailing newlines.
# Case-insensitive for convenience.
NIMHELP_PATTERN = re.compile(r"^[ \t]*<nimhelp>[ \t]*$", re.IGNORECASE)


def is_nimhelp_tag(text: str) -> bool:
    """True iff the message is exactly the literal <nimhelp> (optional surrounding whitespace)."""
    return bool(NIMHELP_PATTERN.search(text))


# Match <nimeffort:level> or <nimeffort:level:budget> ONLY when it's the ENTIRE message
# (after optional leading/trailing whitespace). Avoids false positives from embedded content.
# Accepts named levels (low, medium, high, xhigh, max, ultracode) with optional integer budget.
# Also accepts bare integers (-1 to 1000000) for budget-only mode.
NIMEFFORT_PATTERN = re.compile(
    r"^[ \t]*<nimeffort:\s*(-1|[1-9]\d{0,5}|1000000|low|medium|high|xhigh|max|ultracode)"
    r"(?::\s*(-1|[1-9]\d{0,5}|1000000))?\s*>[ \t]*$",
    re.IGNORECASE
)

# Match <nimeffort> or <nimeffort:status> for showing current effort level
NIMEFFORT_STATUS_PATTERN = re.compile(
    r"^[ \t]*<nimeffort(?::\s*status\s*)?>[ \t]*$",
    re.IGNORECASE
)


def is_nimeffort_tag(text: str) -> bool:
    """Check if message IS exactly the <nimeffort:level> tag (plus optional whitespace).
    Only matches when the entire message is the tag (like <nimrpm:reset>).
    Avoids false positives from embedded content in file outputs, docs, etc.
    """
    return bool(NIMEFFORT_PATTERN.search(text))


def is_nimeffort_status_tag(text: str) -> bool:
    """Check if message IS exactly the <nimeffort> or <nimeffort:status> tag."""
    return bool(NIMEFFORT_STATUS_PATTERN.search(text))


def extract_nimeffort_tag(text: str) -> tuple[str | None, int | None]:
    """Extract effort level and optional budget from <nimeffort:level:budget> tag.
    
    Returns:
        Tuple of (effort_level, budget) where either can be None.
        - <nimeffort:ultracode> → ("ultracode", None)
        - <nimeffort:100> → (None, 100)
        - <nimeffort:ultracode:100> → ("ultracode", 100)
        - No match → (None, None)
    """
    match = NIMEFFORT_PATTERN.search(text)
    if match:
        level = match.group(1).lower() if match.group(1) else None
        # If level is a number (not a named level), treat it as budget
        if level and level.lstrip('-').isdigit():
            return (None, int(level))
        budget = int(match.group(2)) if match.group(2) else None
        return (level, budget)
    return (None, None)


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


# =============================================================================
# NIM Server Type (stream/buffer)
# =============================================================================


def extract_nimserver_tag(text: str) -> str | None:
    """
    Extract server type from <nimserver:stream> or <nimserver:buffer> tag.

    Args:
        text: Message content to search

    Returns:
        'stream' or 'buffer' if found, None otherwise
    """
    match = NIMSERVER_PATTERN.search(text)
    if match:
        return match.group(1).strip().lower()
    return None


def is_nimserver_clear_tag(text: str) -> bool:
    """Check if message contains <nimserver:clear> tag."""
    return bool(NIMSERVER_CLEAR_PATTERN.search(text))


def is_nimserver_message(message: dict) -> bool:
    """
    Check if a message is a nimserver command.

    Args:
        message: Anthropic-format message dict with 'role' and 'content'

    Returns:
        True if user message contains nimserver tag
    """
    if message.get("role") != "user":
        return False

    content = message.get("content", "")
    text = extract_text_from_content(content)
    return extract_nimserver_tag(text) is not None or is_nimserver_clear_tag(text)


def get_nimserver_type(message: dict) -> str | None:
    """
    Get server type from nimserver message, or None if not a nimserver message.
    Returns special string 'CLEAR' for clear command.
    """
    if message.get("role") != "user":
        return None

    content = message.get("content", "")
    text = extract_text_from_content(content)

    if is_nimserver_clear_tag(text):
        return "CLEAR"

    return extract_nimserver_tag(text)
