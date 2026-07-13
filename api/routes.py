"""FastAPI route handlers."""

import json
import traceback
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from loguru import logger

from config.settings import Settings
from providers.base import BaseProvider
from providers.error_mapping import get_user_facing_error_message
from providers.exceptions import (
    InvalidRequestError,
    ProviderError,
    StreamTruncatedError,
)
from providers.logging_utils import build_request_summary, log_request_compact
from providers.request_queue import RequestPriority
from providers.text import extract_text_from_content

from .dependencies import get_provider, get_settings
from .models.anthropic import MessagesRequest, TokenCountRequest
from .models.responses import MessagesResponse, TokenCountResponse, Usage
from .optimization_handlers import optimization_response_to_sse, try_optimizations
from .request_utils import get_token_count
from .swapper import (
    ModelSwapManager,
    NimServerManager,
    extract_modelswap_tag,
    extract_nimserver_tag,
    is_modelswap_clear_tag,
    is_nimhelp_tag,
    is_nimserver_clear_tag,
    is_nimrpm_reset_tag,
    resolve_model_name,
    validate_and_test_model,
)

router = APIRouter()


# =============================================================================
# Model Swapper Helpers
# =============================================================================


def _extract_api_key(request: Request, settings: Settings) -> str:
    """Extract API key from request headers (same logic as middleware)."""
    # Check x-api-key header
    api_key = request.headers.get("x-api-key")
    if api_key:
        return api_key

    # Check Authorization Bearer token
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()

    # Check query parameter
    api_key = request.query_params.get("api_key")
    if api_key:
        return api_key

    # Fallback to proxy API key
    return settings.proxy_api_key


def _is_title_request(request_data: MessagesRequest) -> bool:
    """Check if request is a Claude Code title generation request."""
    if not request_data.system or request_data.tools:
        return False
    system_text = extract_text_from_content(request_data.system).lower()
    return "title" in system_text and "session content" in system_text


def _create_modelswap_response(
    success: bool, model: str, message: str
) -> MessagesResponse:
    """Create Anthropic-format mock response for modelswap result."""
    return MessagesResponse(
        id=f"msg_{uuid.uuid4().hex[:24]}",
        model="model-swapper",
        role="assistant",
        content=[{"type": "text", "text": message}],
        type="message",
        stop_reason="end_turn",
        stop_sequence=None,
        usage=Usage(input_tokens=0, output_tokens=0),
    )


async def _handle_modelswap(
    request: Request,
    request_data: MessagesRequest,
    settings: Settings,
) -> tuple[MessagesResponse | None, str | None]:
    """
    Handle modelswap logic for a request.

    Returns:
        Tuple of (mock_response_dict or None, model_override or None)
        - If mock_response is not None, it's a modelswap command response
        - If model_override is not None, it should be used for the request
    """
    if not settings.swapper_enabled:
        return None, None

    api_key = _extract_api_key(request, settings)

    # Check last user message for modelswap tag
    for msg in reversed(request_data.messages):
        if msg.role == "user":
            # Extract text content from message (can be string or list of content blocks)
            text_content = extract_text_from_content(msg.content)

            # Check for modelswap tag
            model_name = extract_modelswap_tag(text_content)
            is_clear = is_modelswap_clear_tag(text_content)

            if model_name or is_clear:
                # Skip modelswap for title generation requests - they embed
                # <session> content that triggers false detection.
                if _is_title_request(request_data):
                    break

                if is_clear:
                    # Clear the swap
                    await ModelSwapManager.clear(api_key)
                    mock_resp = _create_modelswap_response(
                        True, "", "Model swap cleared - using default model mapping"
                    )
                    return mock_resp, None

                # Resolve short name to full NIM ID before validating/storing
                full_model = resolve_model_name(model_name)

                # Validate and test the model
                success, msg_text = await validate_and_test_model(
                    model_name, settings, api_key
                )
                if success:
                    await ModelSwapManager.set(api_key, full_model)
                    # Build chain: short_name -> resolved_id (if different)
                    if model_name != full_model:
                        chain = f"MODEL MAPPING: '{model_name}' -> `{full_model}`"
                    else:
                        chain = f"MODEL MAPPING: `{full_model}`"
                    mock_resp = _create_modelswap_response(
                        True, full_model, f"Model Updated: {full_model}\n\n{chain}"
                    )
                    return mock_resp, None
                else:
                    mock_resp = _create_modelswap_response(
                        False, model_name, msg_text
                    )
                    return mock_resp, None
            break

    # No modelswap command in this request - check if swap is active
    swapped_model = await ModelSwapManager.get(api_key)
    if swapped_model:
        # Log the full model mapping chain for active swaps
        default_nim = settings.get_model_for_claude(request_data.model)
        logger.info(
            "MODEL MAPPING: '{}' -> '{}' -> `{}`",
            request_data.model,
            default_nim,
            swapped_model,
        )
        return None, swapped_model

    return None, None


# =============================================================================
# Adaptive Rate Limit Reset Helper (<nimrpm:reset>)
# =============================================================================


async def _handle_nimrpm_reset(
    request_data: MessagesRequest,
) -> MessagesResponse | None:
    """
    Check for <nimrpm:reset> tag in the last user message.
    If found, reset the adaptive rate limiter backoff state.

    Returns:
        A mock response if reset was triggered, None otherwise.
    """
    for msg in reversed(request_data.messages):
        if msg.role == "user":
            text_content = extract_text_from_content(msg.content)
            if is_nimrpm_reset_tag(text_content):
                # Skip for title generation requests (false positives)
                if _is_title_request(request_data):
                    return None

                from providers.rate_limit import GlobalRateLimiter

                limiter = GlobalRateLimiter.get_instance()
                limiter.reset_adaptive_backoff()

                return _create_nimserver_response(
                    True,
                    "rpm-reset",
                    "🔄 Adaptive rate limit backoff has been reset.\n\n"
                    "RPM restored to initial value, hold delays cleared.",
                )
            break
    return None


# =============================================================================
# Inline Command Help (<nimhelp>)
# =============================================================================

NIMHELP_TEXT = """NIMbus inline commands — send one as your ENTIRE message:
<modelswap:MODEL>   swap active model for this session (short name auto-resolved + validated live, or full NIM ID)
<modelswap:clear>   clear the model swap, revert to default mapping
<nimserver:stream>  force NIM stream mode for subsequent requests
<nimserver:buffer>  force NIM buffer mode for subsequent requests
<nimserver:clear>   clear server-type override, revert to .env SERVER_TYPE
<nimrpm:reset>      reset adaptive rate-limiter backoff (restore RPM, clear hold delays)
<nimhelp>           show this list
modelswap/nimserver need SWAPPER_ENABLED=true; nimrpm:reset and nimhelp always work.
"""


async def _handle_nimhelp(request_data: MessagesRequest) -> MessagesResponse | None:
    """Return a help-listing mock if the last user message is exactly <nimhelp>."""
    for msg in reversed(request_data.messages):
        if msg.role == "user":
            if is_nimhelp_tag(extract_text_from_content(msg.content)):
                # ponytail: near-shape of _handle_nimrpm_reset; kept separate because
                # nimrpm mutates state, this is pure text — share when a 4th command lands
                if _is_title_request(request_data):
                    return None
                return _create_nimserver_response(True, "help", NIMHELP_TEXT)
            break
    return None


# =============================================================================
# NIM Server Type Swapper Helpers (stream/buffer)
# =============================================================================


def _create_nimserver_response(
    success: bool, server_type: str, message: str
) -> MessagesResponse:
    """Create Anthropic-format mock response for nimserver result."""
    return MessagesResponse(
        id=f"msg_{uuid.uuid4().hex[:24]}",
        model="nim-server-swapper",
        role="assistant",
        content=[{"type": "text", "text": message}],
        type="message",
        stop_reason="end_turn",
        stop_sequence=None,
        usage=Usage(input_tokens=0, output_tokens=0),
    )


async def _handle_nimserver(
    request: Request,
    request_data: MessagesRequest,
    settings: Settings,
) -> tuple[MessagesResponse | None, str | None]:
    """
    Handle nimserver logic for a request.

    Returns:
        Tuple of (mock_response_dict or None, server_type_override or None)
        - If mock_response is not None, it's a nimserver command response
        - If server_type_override is not None, it should be used for the request
    """
    api_key = _extract_api_key(request, settings)

    # Check last user message for nimserver tag
    for msg in reversed(request_data.messages):
        if msg.role == "user":
            text_content = extract_text_from_content(msg.content)

            server_type = extract_nimserver_tag(text_content)
            is_clear = is_nimserver_clear_tag(text_content)

            if server_type or is_clear:
                # Skip nimserver for title generation requests (false positives)
                if _is_title_request(request_data):
                    break

                if is_clear:
                    await NimServerManager.clear(api_key)
                    mock_resp = _create_nimserver_response(
                        True,
                        "",
                        "NIM server type cleared - using default SERVER_TYPE from .env",
                    )
                    return mock_resp, None

                # Store the override
                await NimServerManager.set(api_key, server_type)
                mock_resp = _create_nimserver_response(
                    True,
                    server_type,
                    f"NIM Server Mode: {server_type}\n\n"
                    f"All subsequent requests from this session will use "
                    f"'{server_type}' mode until changed.",
                )
                return mock_resp, None
            break

    # No nimserver command - check if override is active
    server_override = await NimServerManager.get(api_key)
    if server_override:
        logger.info(
            "NIM SERVER OVERRIDE: api_key={} -> '{}'",
            api_key[:8] + "...",
            server_override,
        )
        return None, server_override

    return None, None


# =============================================================================
# Routes
# =============================================================================


@router.post("/v1/messages")
async def create_message(
    request_data: MessagesRequest,
    raw_request: Request,
    provider: BaseProvider = Depends(get_provider),
    settings: Settings = Depends(get_settings),
):
    """Create a message (always streaming)."""

    try:
        if not request_data.messages:
            raise InvalidRequestError("messages cannot be empty")

        # ponytail: the inline-command dispatches below all stream a mock response
        # with 0 input tokens — one helper instead of three copy-pasted blocks.
        def _sse_response(mock: MessagesResponse) -> StreamingResponse:
            request_id = f"req_{uuid.uuid4().hex[:12]}"
            log_request_compact(logger, request_id, request_data)

            async def sse_generator():
                for event in optimization_response_to_sse(mock, 0):
                    yield event

            return StreamingResponse(
                sse_generator(),
                media_type="text/event-stream",
                headers={
                    "X-Accel-Buffering": "no",
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                },
            )

        # Handle model swapper
        mock_response, model_override = await _handle_modelswap(
            raw_request, request_data, settings
        )
        if mock_response is not None:
            return _sse_response(mock_response)

        # Handle NIM server type swapper
        nimserver_mock, server_type_override = await _handle_nimserver(
            raw_request, request_data, settings
        )
        if nimserver_mock is not None:
            return _sse_response(nimserver_mock)

        # Handle adaptive rate limit reset (<nimrpm:reset>)
        rpmreset_mock = await _handle_nimrpm_reset(request_data)
        if rpmreset_mock is not None:
            return _sse_response(rpmreset_mock)

        # Handle inline command help (<nimhelp>)
        nimhelp_mock = await _handle_nimhelp(request_data)
        if nimhelp_mock is not None:
            return _sse_response(nimhelp_mock)

        optimized = try_optimizations(request_data, settings)
        if optimized is not None:
            # Convert optimization response to SSE events for streaming endpoint
            request_id = f"req_{uuid.uuid4().hex[:12]}"
            log_request_compact(logger, request_id, request_data)
            input_tokens = get_token_count(
                request_data.messages, request_data.system, request_data.tools
            )

            async def sse_generator():
                for event in optimization_response_to_sse(optimized, input_tokens):
                    yield event

            return StreamingResponse(
                sse_generator(),
                media_type="text/event-stream",
                headers={
                    "X-Accel-Buffering": "no",
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                },
            )

        request_id = f"req_{uuid.uuid4().hex[:12]}"
        log_request_compact(logger, request_id, request_data)

        input_tokens = get_token_count(
            request_data.messages, request_data.system, request_data.tools
        )

        # Acquire rate limit slot BEFORE streaming (this increments the counter)
        from providers.rate_limit import GlobalRateLimiter

        limiter = GlobalRateLimiter.get_instance()
        await limiter.wait_if_blocked()

        # Show rate limit status (NVIDIA NIM only)
        # Now log the status AFTER the slot has been acquired
        status = limiter.get_status()
        current = status["current"]
        max_req = status["max"]
        remaining = status["remaining"]
        reset_in = status["reset_in_seconds"]

        # Only log if there are active requests (skip the 0/40 case)
        if current > 0:
            # Calculate percentage and choose emoji
            percentage = (current / max_req) * 100
            if percentage >= 90:
                emoji = "🔴"
            elif percentage >= 70:
                emoji = "🟡"
            else:
                emoji = "🟢"

            # Create visual bar
            bar_width = 20
            filled = int((current / max_req) * bar_width)
            bar = "█" * filled + "░" * (bar_width - filled)

            # Use print to ensure it shows up (only show reset time if active)
            if reset_in > 0:
                print(
                    f"{emoji} Rate Limit: [{bar}] {current}/{max_req} ({percentage:.0f}%) | {remaining} left | Resets in {reset_in:.1f}s",
                    flush=True,
                )
            else:
                print(
                    f"{emoji} Rate Limit: [{bar}] {current}/{max_req} ({percentage:.0f}%) | {remaining} left",
                    flush=True,
                )

        # If nimserver override is active and it's 'buffer', dispatch to buffered_request
        # and convert the response to SSE for the streaming endpoint.
        if server_type_override == "buffer":
            logger.info("NIMSERVER: streaming endpoint -> using buffered mode (override)")

            response = await provider.buffered_request(
                request_data,
                input_tokens=input_tokens,
                request_id=request_id,
                model_override=model_override,
                priority=RequestPriority.NORMAL,
            )

            async def sse_generator():
                for event in optimization_response_to_sse(response, input_tokens):
                    yield event

            return StreamingResponse(
                sse_generator(),
                media_type="text/event-stream",
                headers={
                    "X-Accel-Buffering": "no",
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                },
            )

        # Normal streaming path
        return StreamingResponse(
            provider.stream_response(
                request_data,
                input_tokens=input_tokens,
                request_id=request_id,
                model_override=model_override,
                priority=RequestPriority.NORMAL,
            ),
            media_type="text/event-stream",
            headers={
                "X-Accel-Buffering": "no",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
            },
        )

    except ProviderError:
        raise
    except Exception as e:
        logger.error(f"Error: {e!s}\n{traceback.format_exc()}")
        raise HTTPException(
            status_code=getattr(e, "status_code", 500),
            detail=get_user_facing_error_message(e),
        ) from e


@router.post("/v1/messages/buffered")
async def create_message_buffered(
    request_data: MessagesRequest,
    raw_request: Request,
    provider: BaseProvider = Depends(get_provider),
    settings: Settings = Depends(get_settings),
):
    """Create a message (buffered/non-streaming mode).

    Collects the full response before returning as JSON, with automatic
    retry on transient NVIDIA backend connection drops. No streaming involved.
    """
    try:
        if not request_data.messages:
            raise InvalidRequestError("messages cannot be empty")

        # Handle model swapper
        mock_response, model_override = await _handle_modelswap(
            raw_request, request_data, settings
        )
        if mock_response is not None:
            request_id = f"req_{uuid.uuid4().hex[:12]}"
            log_request_compact(logger, request_id, request_data)
            return JSONResponse(
                content=mock_response,
                headers={
                    "X-Buffered": "true",
                    "X-Request-ID": request_id,
                },
            )

        # Handle NIM server type swapper
        nimserver_mock, server_type_override = await _handle_nimserver(
            raw_request, request_data, settings
        )
        if nimserver_mock is not None:
            request_id = f"req_{uuid.uuid4().hex[:12]}"
            log_request_compact(logger, request_id, request_data)
            return JSONResponse(
                content=nimserver_mock,
                headers={
                    "X-Buffered": "true",
                    "X-Request-ID": request_id,
                },
            )

        # Handle adaptive rate limit reset (<nimrpm:reset>)
        rpmreset_mock = await _handle_nimrpm_reset(request_data)
        if rpmreset_mock is not None:
            request_id = f"req_{uuid.uuid4().hex[:12]}"
            log_request_compact(logger, request_id, request_data)
            return JSONResponse(
                content=rpmreset_mock,
                headers={
                    "X-Buffered": "true",
                    "X-Request-ID": request_id,
                },
            )

        # Handle inline command help (<nimhelp>)
        nimhelp_mock = await _handle_nimhelp(request_data)
        if nimhelp_mock is not None:
            request_id = f"req_{uuid.uuid4().hex[:12]}"
            log_request_compact(logger, request_id, request_data)
            return JSONResponse(
                content=nimhelp_mock,
                headers={
                    "X-Buffered": "true",
                    "X-Request-ID": request_id,
                },
            )

        optimized = try_optimizations(request_data, settings)
        if optimized is not None:
            return optimized

        request_id = f"req_{uuid.uuid4().hex[:12]}"
        log_request_compact(logger, request_id, request_data)

        input_tokens = get_token_count(
            request_data.messages, request_data.system, request_data.tools
        )

        # Acquire rate limit slot (same as streaming path)
        from providers.rate_limit import GlobalRateLimiter

        limiter = GlobalRateLimiter.get_instance()
        await limiter.wait_if_blocked()

        # Log rate limit status (same as streaming path)
        status = limiter.get_status()
        current = status["current"]
        max_req = status["max"]
        remaining = status["remaining"]
        reset_in = status["reset_in_seconds"]

        if current > 0:
            percentage = (current / max_req) * 100
            if percentage >= 90:
                emoji = "🔴"
            elif percentage >= 70:
                emoji = "🟡"
            else:
                emoji = "🟢"

            bar_width = 20
            filled = int((current / max_req) * bar_width)
            bar = "█" * filled + "░" * (bar_width - filled)

            if reset_in > 0:
                print(
                    f"{emoji} Rate Limit: [{bar}] {current}/{max_req} ({percentage:.0f}%) | {remaining} left | Resets in {reset_in:.1f}s | BUFFERED",
                    flush=True,
                )
            else:
                print(
                    f"{emoji} Rate Limit: [{bar}] {current}/{max_req} ({percentage:.0f}%) | {remaining} left | BUFFERED",
                    flush=True,
                )

        # If nimserver override is active and it's 'stream', dispatch to stream_response
        # and collect the result into a JSON response for the buffered endpoint.
        if server_type_override == "stream":
            logger.info("NIMSERVER: buffered endpoint -> using streaming mode (override)")

            full_response_text = ""
            async for chunk in provider.stream_response(
                request_data,
                input_tokens=input_tokens,
                request_id=request_id,
                model_override=model_override,
            ):
                if chunk.strip():
                    try:
                        event_data = chunk.split("data: ", 1)[-1].strip()
                        data = json.loads(event_data)
                        if data.get("type") == "content_block_delta":
                            delta = data.get("delta", {})
                            if delta.get("type") == "text_delta":
                                full_response_text += delta.get("text", "")
                    except Exception:
                        continue

            # Build an Anthropic-format response from the collected text
            import uuid as _uuid

            response = {
                "id": f"msg_{_uuid.uuid4().hex[:24]}",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": full_response_text.strip()}],
                "model": request_data.model,
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {
                    "input_tokens": input_tokens,
                    "output_tokens": 0,
                },
            }

            return JSONResponse(
                content=response,
                headers={
                    "X-Buffered": "true",
                    "X-Request-ID": request_id,
                },
            )

        # Normal buffered path
        response = await provider.buffered_request(
            request_data,
            input_tokens=input_tokens,
            request_id=request_id,
            model_override=model_override,
            priority=RequestPriority.NORMAL,
        )

        return JSONResponse(
            content=response,
            headers={
                "X-Buffered": "true",
                "X-Request-ID": request_id,
            },
        )

    except StreamTruncatedError as e:
        logger.error(f"BUFFERED_ERROR: {e!s}\n{traceback.format_exc()}")
        raise HTTPException(
            status_code=502,
            detail=f"NVIDIA backend connection issue: {e!s}",
        ) from e
    except ProviderError:
        raise
    except Exception as e:
        logger.error(f"Error: {e!s}\n{traceback.format_exc()}")
        raise HTTPException(
            status_code=getattr(e, "status_code", 500),
            detail=get_user_facing_error_message(e),
        ) from e


@router.post("/v1/messages/count_tokens")
async def count_tokens(request_data: TokenCountRequest):
    """Count tokens for a request."""
    request_id = f"req_{uuid.uuid4().hex[:12]}"
    with logger.contextualize(request_id=request_id):
        try:
            tokens = get_token_count(
                request_data.messages, request_data.system, request_data.tools
            )
            summary = build_request_summary(request_data)
            summary["request_id"] = request_id
            summary["input_tokens"] = tokens
            logger.info("COUNT_TOKENS: {}", json.dumps(summary))
            return TokenCountResponse(input_tokens=tokens)
        except Exception as e:
            logger.error(
                "COUNT_TOKENS_ERROR: request_id={} error={}\n{}",
                request_id,
                get_user_facing_error_message(e),
                traceback.format_exc(),
            )
            raise HTTPException(
                status_code=500, detail=get_user_facing_error_message(e)
            ) from e


@router.get("/")
async def root(settings: Settings = Depends(get_settings)):
    """Root endpoint."""
    return {
        "status": "ok",
        "provider": "nvidia_nim",
        "model": settings.model,
        "model_list": settings.model_list,
        "model_mapping": {
            "sonnet_opening": " ".join(
                [
                    "Position 1 (Sonnet 4.6 / Default)",
                    "- maps to model_list[0] if 1+ models configured",
                ]
            ),
            "opus_opening": " ".join(
                [
                    "Position 2 (Opus 4.7)",
                    "- maps to model_list[0] if 1 model, model_list[1] if 2+ models",
                ]
            ),
            "haiku_opening": " ".join(
                ["Position 3 (Haiku 4.5)", "- maps to model_list[last] based on count"]
            ),
        },
    }


@router.get("/health")
async def health():
    """Health check endpoint."""
    return {"status": "healthy"}


@router.get("/status")
async def status():
    """Get current rate limit and queue status."""
    from providers.rate_limit import GlobalRateLimiter
    from providers.base import BaseProvider

    limiter = GlobalRateLimiter.get_instance()
    status_data = limiter.get_status()

    # Try to include queue stats if available
    provider = getattr(status, '_provider', None)
    if provider is None:
        # Try to get provider from app state
        try:
            from api.dependencies import get_provider
            # Can't easily get provider here without request, skip for now
            pass
        except Exception:
            pass

    # Add queue stats if we can access the provider
    # For now, return rate limiter status
    return status_data


@router.get("/queue/status")
async def queue_status(request: Request):
    """Get current request queue status."""
    from api.dependencies import get_provider

    provider = get_provider()
    if hasattr(provider, '_request_queue') and provider._request_queue:
        return provider._request_queue.get_stats()
    return {"error": "Request queue not initialized"}


@router.post("/stop")
async def stop_cli(request: Request):
    """Stop all CLI sessions and pending tasks."""
    handler = getattr(request.app.state, "message_handler", None)
    if not handler:
        # Fallback if messaging not initialized
        cli_manager = getattr(request.app.state, "cli_manager", None)
        if cli_manager:
            await cli_manager.stop_all()
            logger.info("STOP_CLI: source=cli_manager cancelled_count=N/A")
            return {"status": "stopped", "source": "cli_manager"}
        raise HTTPException(status_code=503, detail="Messaging system not initialized")

    count = await handler.stop_all_tasks()
    logger.info("STOP_CLI: source=handler cancelled_count={}", count)
    return {"status": "stopped", "cancelled_count": count}