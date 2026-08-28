"""Streaming parser for DeepSeek DSML tool-call markup.

DeepSeek V4 Pro sometimes emits tool calls as literal DSML markup inside
delta.content instead of structured tool_calls. This parser buffers the stream
and converts complete <tool_calls> blocks into Anthropic-format tool_use dicts.
"""

import json
import re
import uuid

from loguru import logger


class DsmlParser:
    def __init__(self):
        self._buffer = ""
        self._in_tool_calls = False

    # Tag constants for canonical form (based on actual logs)
    _OPEN_TC = "｜DSML｜tool_calls>"
    _CLOSE_TC = "</｜DSML｜tool_calls>"
    _OPEN_INVOKE = "｜DSML｜invoke name=\""
    _CLOSE_INVOKE = "</｜DSML｜invoke>"
    _OPEN_PARAM = "｜DSML｜parameter name=\""
    _CLOSE_PARAM = "</｜DSML｜parameter>"

    def _normalize(self, text: str) -> str:
        # Normalize degraded DSML token variants: <||DSML|| -> <｜DSML｜
        # Handle both single and double pipe variants with optional whitespace
        normalized = re.sub(
            r'<\s*/?\s*\|\s*\|\s*DSML\s*\|\s*\|\s*>',
            lambda m: m.group(0).replace('||', '｜'),
            text
        )
        normalized = re.sub(
            r'<\s*/?\s*｜\s*｜\s*DSML\s*｜\s*｜\s*>',
            lambda m: m.group(0).replace('｜｜', '｜'),
            normalized
        )
        return normalized

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
                idx = self._buffer.find(self._OPEN_TC)
                if idx == -1:
                    # No opening tag found - emit all but a possible partial opening tag tail
                    safe_text = self._safe_text_tail()
                    out_text_parts.append(safe_text)
                    self._buffer = self._buffer[len(safe_text):]
                    break

                # Found opening tag - emit text before it
                out_text_parts.append(self._buffer[:idx])
                self._buffer = self._buffer[idx + len(self._OPEN_TC):]
                self._in_tool_calls = True
                # Continue loop to parse tool_calls content
            else:
                # We're inside tool_calls, look for closing tag
                end = self._buffer.find(self._CLOSE_TC)
                if end == -1:
                    # No closing tag yet - wait for more data
                    break

                # Found closing tag - extract and parse the tool_calls content
                block = self._buffer[:end]
                self._buffer = self._buffer[end + len(self._CLOSE_TC):]
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
        if self._OPEN_TC.startswith(remaining):
            # This could be a partial opening tag - keep it in buffer
            return self._buffer[:last_lt]
        else:
            # This '<' is definitely not the start of our tag - safe to emit everything
            return self._buffer

    def _parse_block(self, block: str) -> list[dict]:
        """
        Parse a complete tool_calls block into a list of tool_use dicts.

        Expected format:
        <｜DSML｜tool_calls>
          <｜DSML｜invoke name="tool_name">
            <｜DSML｜parameter name="param1" string="value1">...</｜DSML｜parameter>
            <｜DSML｜parameter name="param2" string="value2">...</｜DSML｜parameter>
          </｜DSML｜invoke>
        </｜DSML｜tool_calls>
        """
        tools = []

        # Normalize the block content
        block = self._normalize(block)

        # Find all invoke tags
        invoke_start_pos = 0
        while True:
            invoke_start = block.find(self._OPEN_INVOKE, invoke_start_pos)
            if invoke_start == -1:
                break

            # Extract tool name
            name_start = invoke_start + len(self._OPEN_INVOKE)
            name_end = block.find("\"", name_start)
            if name_end == -1:
                # Malformed - skip this invoke
                invoke_start_pos = invoke_start + 1
                continue
            tool_name = block[name_start:name_end]

            # Find closing invoke tag
            invoke_end = block.find(self._CLOSE_INVOKE, name_end)
            if invoke_end == -1:
                # Malformed - skip this invoke
                invoke_start_pos = name_end + 1
                continue

            # Extract the invoke body (between > and </invoke>)
            invoke_body = block[name_end + 1:invoke_end]

            # Parse parameters from the invoke body
            parameters = self._parse_parameters(invoke_body)

            # Create tool use dict
            tools.append({
                "type": "tool_use",
                "id": f"toolu_dsml_{uuid.uuid4().hex[:8]}",
                "name": tool_name,
                "input": parameters,
            })

            # Move position past this invoke
            invoke_start_pos = invoke_end + len(self._CLOSE_INVOKE)

        return tools

    def _parse_parameters(self, text: str) -> dict:
        """
        Parse parameter tags from text.

        Expected format:
        <｜DSML｜parameter name="param1" string="value1">...</｜DSML｜parameter>
        """
        parameters = {}
        param_start_pos = 0

        while True:
            param_start = text.find(self._OPEN_PARAM, param_start_pos)
            if param_start == -1:
                break

            # Extract parameter name
            name_start = param_start + len(self._OPEN_PARAM)
            name_end = text.find("\"", name_start)
            if name_end == -1:
                # Malformed - skip this parameter
                param_start_pos = param_start + 1
                continue
            param_name = text[name_start:name_end]

            # Find the string="..." part
            string_start = text.find("string=\"", name_end)
            if string_start == -1:
                # Malformed - skip this parameter
                param_start_pos = param_start + 1
                continue
            string_value_start = string_start + len("string=\"")
            string_value_end = text.find("\"", string_value_start)
            if string_value_end == -1:
                # Malformed - skip this parameter
                param_start_pos = string_start + 1
                continue
            param_value = text[string_value_start:string_value_end]

            # Find closing parameter tag
            param_end = text.find(self._CLOSE_PARAM, string_value_end)
            if param_end == -1:
                # Malformed - skip this parameter
                param_start_pos = string_start + 1
                continue

            # Store parameter
            parameters[param_name] = param_value

            # Move position past this parameter
            param_start_pos = param_end + len(self._CLOSE_PARAM)

        return parameters

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