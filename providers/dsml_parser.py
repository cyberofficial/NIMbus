"""DSML tool-call markup parsing for DeepSeek-V4.

DeepSeek V4 sometimes emits tool calls as literal DSML markup inside
message content instead of structured tool_calls. The emitted tokens can
also be "degraded" from the canonical full-width form (<｜DSML｜tool_calls>)
to an ASCII double-pipe form (<||DSML||tool_calls>) without the newlines
the reference format expects. This module normalizes both variants and
converts complete <tool_calls> blocks into Anthropic-format tool_use dicts.
"""

import json
import re
import uuid

DSML_TOKEN = "｜DSML｜"

_OPEN_TC = f"<{DSML_TOKEN}tool_calls>"
_CLOSE_TC = f"</{DSML_TOKEN}tool_calls>"
_CLOSE_INVOKE = f"</{DSML_TOKEN}invoke>"

# Degraded equivalent of the opening tag, used for partial-tag detection
# when a tag is split across stream chunks.
_DEGRADED_OPEN = "<||DSML||tool_calls>"

# Degraded tokens: <||DSML||tag>, <｜｜DSML｜｜tag>, mixed pipes/whitespace.
# The tag name and any attributes that follow are preserved as-is, so both
# bare tags (<||DSML||tool_calls>) and attributed tags
# (<||DSML||invoke name="x">) are normalized.
_DEGRADED_TOKEN_RE = re.compile(
    r"<\s*(/?)\s*(?:\|\s*\||｜\s*｜)\s*DSML\s*(?:\|\s*\||｜\s*｜)\s*([A-Za-z_][A-Za-z0-9_]*)"
)

_TOOL_CALLS_BLOCK_RE = re.compile(
    re.escape(_OPEN_TC) + r"(.*?)" + re.escape(_CLOSE_TC),
    re.DOTALL,
)
_INVOKE_OPEN_RE = re.compile(re.escape(f"<{DSML_TOKEN}invoke") + r"([^>]*)>")
_PARAM_RE = re.compile(
    re.escape(f"<{DSML_TOKEN}parameter")
    + r"([^>]*)>(.*?)"
    + re.escape(f"</{DSML_TOKEN}parameter>"),
    re.DOTALL,
)
_ATTR_NAME_RE = re.compile(r'name="([^"]*)"')
_ATTR_STRING_RE = re.compile(r'string="([^"]*)"')


def is_dsml_model(model: str | None) -> bool:
    """DSML tool-call markup is a DeepSeek-V4 family output format."""
    return bool(model) and model.startswith("deepseek-ai/deepseek-v4")


def normalize_dsml_markup(text: str) -> str:
    """Convert degraded DSML tokens like <||DSML||tool_calls> to <｜DSML｜tool_calls>."""
    return _DEGRADED_TOKEN_RE.sub(
        lambda m: f"<{m.group(1)}{DSML_TOKEN}{m.group(2)}", text
    )


def _param_value(raw: str, type_flag: str | None):
    """Convert a parameter's inner text to its typed value.

    string="true" marks a plain-string value; otherwise the text is
    JSON-typed and falls back to the raw string if it doesn't parse.
    """
    value = raw.strip()
    if type_flag == "true":
        return value
    try:
        return json.loads(value)
    except ValueError:
        return value


def _parse_invokes(block: str) -> list[dict]:
    """Parse <｜DSML｜invoke> elements from a tool_calls block body."""
    tools = []
    opens = list(_INVOKE_OPEN_RE.finditer(block))
    for i, m in enumerate(opens):
        name_match = _ATTR_NAME_RE.search(m.group(1))
        if not name_match:
            continue

        # Invoke body: from end of the open tag up to the closing tag,
        # never spilling into the next invoke.
        body_start = m.end()
        body_end = opens[i + 1].start() if i + 1 < len(opens) else len(block)
        close = block.find(_CLOSE_INVOKE, body_start)
        if close != -1 and close < body_end:
            body_end = close

        parameters = {}
        for pm in _PARAM_RE.finditer(block[body_start:body_end]):
            pname = _ATTR_NAME_RE.search(pm.group(1))
            if not pname:
                continue
            flag = _ATTR_STRING_RE.search(pm.group(1))
            parameters[pname.group(1)] = _param_value(
                pm.group(2), flag.group(1) if flag else None
            )

        tools.append(
            {
                "type": "tool_use",
                "id": f"toolu_dsml_{uuid.uuid4().hex[:8]}",
                "name": name_match.group(1),
                "input": parameters,
            }
        )
    return tools


_RESIDUAL_DSML_TAG_RE = re.compile(r"</?\s*｜DSML｜\s*[A-Za-z_][^<>]*>")


def _strip_residual_tags(text: str) -> str:
    """Remove DSML tags that survived outside parsed tool_calls blocks.

    Backends with a server-side tool-call parser sometimes consume the head
    of a DSML block and leave dangling closing tags (e.g. only
    </｜DSML｜parameter></｜DSML｜invoke></｜DSML｜tool_calls>) in content.
    Those tags are never legitimate user-facing text.
    """
    return _RESIDUAL_DSML_TAG_RE.sub("", text)


def parse_dsml_tool_calls(text: str) -> tuple[str, list[dict]]:
    """Extract DSML tool_calls blocks from completion text.

    Returns (remaining_text, tool_use_blocks) where remaining_text is the
    content outside the tool_calls blocks. If no tool calls are found the
    input is returned (normalized, residual tags stripped) unchanged.
    """
    text = normalize_dsml_markup(text)
    tools: list[dict] = []
    remaining_parts: list[str] = []
    last = 0
    for m in _TOOL_CALLS_BLOCK_RE.finditer(text):
        remaining_parts.append(text[last : m.start()])
        tools.extend(_parse_invokes(m.group(1)))
        last = m.end()
    remaining_parts.append(text[last:])
    remaining = _strip_residual_tags("".join(remaining_parts)).strip()
    if not tools:
        return remaining, []
    return remaining, tools


class DsmlParser:
    """Stateful streaming parser: feed chunks, get text + tool_use blocks back.

    Buffers across chunk boundaries so tags split over multiple deltas are
    still recognized.
    """

    def __init__(self):
        self._buffer = ""
        self._in_tool_calls = False

    def _normalize(self, text: str) -> str:
        return normalize_dsml_markup(text)

    def feed(self, text: str) -> tuple[str, list[dict]]:
        """
        Feed text into the parser.
        Returns a tuple of (filtered_text, detected_tool_calls).

        filtered_text: Text that should be passed through as normal message content.
        detected_tools: List of Anthropic-format tool_use blocks.
        """
        if not text:
            return "", []

        # Normalize the text to handle degraded token variants
        self._buffer = self._normalize(self._buffer + text)
        out_text_parts = []
        tools = []

        while self._buffer:
            if not self._in_tool_calls:
                # Look for the opening tool_calls tag
                idx = self._buffer.find(_OPEN_TC)
                if idx == -1:
                    # No opening tag found - emit all but a possible partial opening tag tail
                    safe_text = self._safe_text_tail()
                    out_text_parts.append(safe_text)
                    self._buffer = self._buffer[len(safe_text):]
                    break

                # Found opening tag - emit text before it
                out_text_parts.append(self._buffer[:idx])
                self._buffer = self._buffer[idx + len(_OPEN_TC):]
                self._in_tool_calls = True
                # Continue loop to parse tool_calls content
            else:
                # We're inside tool_calls, look for closing tag
                end = self._buffer.find(_CLOSE_TC)
                if end == -1:
                    # No closing tag yet - wait for more data
                    break

                # Found closing tag - extract and parse the tool_calls content
                block = self._buffer[:end]
                self._buffer = self._buffer[end + len(_CLOSE_TC):]
                self._in_tool_calls = False
                tools.extend(self._parse_block(block))

        return "".join(out_text_parts), tools

    def _safe_text_tail(self) -> str:
        """
        If the buffer ends with an incomplete "<..." sequence that could be the
        start of a tag, keep that fragment in the buffer and return the safe-to-emit prefix.

        This prevents emitting partial tags that might be completed in future chunks.
        """
        # Find the last '<' in the buffer
        last_lt = self._buffer.rfind("<")
        if last_lt == -1:
            # No '<' at all - safe to emit everything
            return self._buffer

        # Check if what follows could be the start of our opening tag
        remaining = self._buffer[last_lt:]

        # Check if remaining could be a prefix of our opening tag
        # (both canonical and degraded forms, since a tag may be split
        # across chunk boundaries before normalization can see it)
        if _OPEN_TC.startswith(remaining) or _DEGRADED_OPEN.startswith(remaining):
            # This could be a partial opening tag - keep it in buffer
            return self._buffer[:last_lt]
        else:
            # This '<' is definitely not the start of our tag - safe to emit everything
            return self._buffer

    def _parse_block(self, block: str) -> list[dict]:
        """Parse a complete tool_calls block body into tool_use dicts."""
        return _parse_invokes(self._normalize(block))

    def flush(self) -> list[dict]:
        """
        Flush any remaining tool calls in the buffer.
        Attempts to parse any leftover buffer as a tool_calls block.
        """
        if not self._buffer or not self._in_tool_calls:
            return []

        # Try to parse whatever we have as a tool_calls block
        # This handles the case where the stream ends without a closing tag
        tools = self._parse_block(self._buffer)
        self._buffer = ""
        self._in_tool_calls = False
        return tools