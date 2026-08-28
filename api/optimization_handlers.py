"""Optimization handlers for fast-path API responses.

Each handler returns a MessagesResponse if the request matches and the
optimization is enabled, otherwise None.
"""

import json
import uuid

from loguru import logger

from config.settings import Settings

from .command_utils import extract_command_prefix, extract_filepaths_from_command
from .detection import (
    is_filepath_extraction_request,
    is_prefix_detection_request,
    is_quota_check_request,
    is_recap_request,
    is_suggestion_mode_request,
    is_title_generation_request,
)
from .models.anthropic import MessagesRequest
from .models.responses import MessagesResponse, Usage
from providers.sse_builder import SSEBuilder


def optimization_response_to_sse(response: MessagesResponse | dict, input_tokens: int = 0):
    """Convert an optimization MessagesResponse to SSE events for streaming.

    Generates the proper SSE sequence: message_start -> content_block_start -> content_block_delta -> content_block_stop -> message_delta -> message_stop
    """
    # Helper to get attribute from object or dict
    def get_attr(obj, attr, default=None):
        if hasattr(obj, attr):
            return getattr(obj, attr)
        elif isinstance(obj, dict):
            return obj.get(attr, default)
        return default

    # Extract response fields
    response_id = get_attr(response, 'id')
    response_model = get_attr(response, 'model')
    response_content = get_attr(response, 'content')
    response_stop_reason = get_attr(response, 'stop_reason', 'end_turn')
    response_usage = get_attr(response, 'usage')

    sse = SSEBuilder(response_id, response_model, input_tokens)

    # message_start
    yield sse.message_start()

    # Emit ALL content blocks (text + tool_use). Dropping non-text blocks here
    # caused Claude Code to receive stop_reason=tool_use with no tool call.
    if response_content:
        for index, block in enumerate(response_content):
            block_type = get_attr(block, "type")

            if block_type == "text":
                block_text = get_attr(block, "text", "")
                if not block_text:
                    continue
                yield sse.content_block_start(index, "text", text=block_text)
                yield sse.content_block_delta(index, "text_delta", block_text)
                yield sse.content_block_stop(index)

            elif block_type == "thinking":
                thinking_text = get_attr(block, "thinking", "")
                if not thinking_text:
                    continue
                yield sse.content_block_start(index, "thinking", thinking=thinking_text)
                yield sse.content_block_delta(index, "thinking_delta", thinking_text)
                yield sse.content_block_stop(index)

            elif block_type == "tool_use":
                tool_id = get_attr(block, "id", f"tool_{uuid.uuid4()}")
                tool_name = get_attr(block, "name", "")
                tool_input = get_attr(block, "input", {})
                yield sse.content_block_start(
                    index, "tool_use", id=tool_id, name=tool_name
                )
                if tool_input:
                    yield sse.content_block_delta(
                        index, "input_json_delta", json.dumps(tool_input)
                    )
                yield sse.content_block_stop(index)

            else:
                logger.warning("Unsupported content block type {}: dropped", block_type)

    # message_delta with stop_reason
    stop_reason = response_stop_reason
    output_tokens = 0
    if response_usage:
        output_tokens = get_attr(response_usage, 'output_tokens', 0)
    yield sse.message_delta(stop_reason, output_tokens)

    # message_stop
    yield sse.message_stop()


def try_prefix_detection(
    request_data: MessagesRequest, settings: Settings
) -> MessagesResponse | None:
    """Fast prefix detection - return command prefix without API call."""
    if not settings.fast_prefix_detection:
        return None

    is_prefix_req, command = is_prefix_detection_request(request_data)
    if not is_prefix_req:
        return None

    logger.info("Optimization: Fast prefix detection request")
    return MessagesResponse(
        id=f"msg_{uuid.uuid4()}",
        model=request_data.model,
        content=[{"type": "text", "text": extract_command_prefix(command)}],
        stop_reason="end_turn",
        usage=Usage(input_tokens=100, output_tokens=5),
    )


def try_quota_mock(
    request_data: MessagesRequest, settings: Settings
) -> MessagesResponse | None:
    """Mock quota probe requests."""
    if not settings.enable_network_probe_mock:
        return None
    if not is_quota_check_request(request_data):
        return None

    logger.info("Optimization: Intercepted and mocked quota probe")
    return MessagesResponse(
        id=f"msg_{uuid.uuid4()}",
        model=request_data.model,
        role="assistant",
        content=[{"type": "text", "text": "Quota check passed."}],
        stop_reason="end_turn",
        usage=Usage(input_tokens=10, output_tokens=5),
    )


def try_title_skip(
    request_data: MessagesRequest, settings: Settings
) -> MessagesResponse | None:
    """Skip title generation requests."""
    if not settings.enable_title_generation_skip:
        return None
    if not is_title_generation_request(request_data):
        return None

    logger.info("Optimization: Skipped title generation request")
    return MessagesResponse(
        id=f"msg_{uuid.uuid4()}",
        model=request_data.model,
        role="assistant",
        content=[{"type": "text", "text": "Conversation"}],
        stop_reason="end_turn",
        usage=Usage(input_tokens=100, output_tokens=5),
    )


def try_suggestion_skip(
    request_data: MessagesRequest, settings: Settings
) -> MessagesResponse | None:
    """Skip suggestion mode requests.

    DISABLED: Current detection is too broad and causes false positives.
    Re-enable when better detection logic is implemented.
    """
    # Temporarily disabled - re-enable when detection is improved
    _ = request_data  # Avoid unused param warning
    _ = settings
    return None
    # Original implementation preserved for future reference:
    # if not settings.enable_suggestion_mode_skip:
    #     return None
    # if not is_suggestion_mode_request(request_data):
    #     return None
    # logger.info("Optimization: Skipped suggestion mode request")
    # return MessagesResponse(
    #     id=f"msg_{uuid.uuid4()}",
    #     model=request_data.model,
    #     role="assistant",
    #     content=[{"type": "text", "text": ""}],
    #     stop_reason="end_turn",
    #     usage=Usage(input_tokens=100, output_tokens=1),
    # )


def try_filepath_mock(
    request_data: MessagesRequest, settings: Settings
) -> MessagesResponse | None:
    """Mock filepath extraction requests."""
    if not settings.enable_filepath_extraction_mock:
        return None

    is_fp, cmd, output = is_filepath_extraction_request(request_data)
    if not is_fp:
        return None

    filepaths = extract_filepaths_from_command(cmd, output)
    logger.info("Optimization: Mocked filepath extraction")
    return MessagesResponse(
        id=f"msg_{uuid.uuid4()}",
        model=request_data.model,
        role="assistant",
        content=[{"type": "text", "text": filepaths}],
        stop_reason="end_turn",
        usage=Usage(input_tokens=100, output_tokens=10),
    )


def try_recap_skip(
    request_data: MessagesRequest, settings: Settings
) -> MessagesResponse | None:
    """Skip Claude Code recap requests - user returned after stepping away.

    DISABLED: Currently blocks legitimate recap requests.
    Re-enable when better detection logic is implemented.
    """
    # Temporarily disabled - re-enable when detection is improved
    _ = request_data  # Avoid unused param warning
    _ = settings
    return None
    # Original implementation preserved for future reference:
    # if not settings.enable_recap_skip:
    #     return None
    # if not is_recap_request(request_data):
    #     return None
    # logger.info("Claude requested a recap, Blocked")
    # return MessagesResponse(
    #     id=f"msg_{uuid.uuid4()}",
    #     model=request_data.model,
    #     role="assistant",
    #     content=[{"type": "text", "text": "Recap blocked by NIMbus."}],
    #     stop_reason="end_turn",
    #     usage=Usage(input_tokens=1, output_tokens=1),
    # )


# Cheapest/most common optimizations first for faster short-circuit.
OPTIMIZATION_HANDLERS = [
    try_recap_skip,
    try_quota_mock,
    try_prefix_detection,
    try_title_skip,
    try_suggestion_skip,
    try_filepath_mock,
]


def try_optimizations(
    request_data: MessagesRequest, settings: Settings
) -> MessagesResponse | None:
    """Run optimization handlers in order. Returns first match or None."""
    for handler in OPTIMIZATION_HANDLERS:
        result = handler(request_data, settings)
        if result is not None:
            return result
    return None
