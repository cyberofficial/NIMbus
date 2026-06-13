"""Optimization handlers for fast-path API responses.

Each handler returns a MessagesResponse if the request matches and the
optimization is enabled, otherwise None.
"""

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


def optimization_response_to_sse(response: MessagesResponse, input_tokens: int = 0):
    """Convert an optimization MessagesResponse to SSE events for streaming.

    Generates the proper SSE sequence: message_start -> content_block_start -> content_block_delta -> content_block_stop -> message_delta -> message_stop
    """
    sse = SSEBuilder(response.id, response.model, input_tokens)

    # message_start
    yield sse.message_start()

    # content_block_start for text
    content = response.content
    if content:
        text_content = ""
        for block in content:
            # Handle both dict and Pydantic model
            if hasattr(block, 'type'):
                block_type = block.type
                block_text = block.text if hasattr(block, 'text') else ""
            else:
                block_type = block.get("type", "")
                block_text = block.get("text", "")
            if block_type == "text":
                text_content = block_text
                break

        yield sse.content_block_start(0, "text", text=text_content)

        if text_content:
            yield sse.content_block_delta(0, "text_delta", text_content)

        yield sse.content_block_stop(0)

    # message_delta with stop_reason
    yield sse.message_delta(response.stop_reason, response.usage.output_tokens)

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
