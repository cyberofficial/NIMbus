"""NVIDIA NIM provider implementation.

This module contains the NIM provider which handles:
- OpenAI client configuration for NVIDIA NIM API
- Request body building with NIM-specific parameters
- Streaming response handling with SSE format conversion
"""

import asyncio
import json
import random
import sys
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
from loguru import logger
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    BadRequestError,
    InternalServerError,
    RateLimitError,
    UnprocessableEntityError,
)

# HTTP status codes that should trigger a retry (5xx server errors)
_RETRYABLE_HTTP_STATUS = {500, 502, 503, 504}

from config.nim import NimSettings
from providers.base import BaseProvider, ProviderConfig
from providers.error_mapping import (
    append_request_id,
    get_user_facing_error_message,
    map_error,
)
from providers.exceptions import StreamTruncatedError
from providers.header_capture import (
    CapturedHeaders,
    HeaderCapturingTransport,
    request_id_var,
)
from providers.heuristic_tool_parser import HeuristicToolParser
from providers.rate_limit import GlobalRateLimiter
from providers.request import build_request_body
from providers.request_queue import RequestPriority, RequestQueue
from providers.sse_builder import SSEBuilder, map_stop_reason
from providers.think_parser import ContentType, ThinkTagParser

NVIDIA_NIM_BASE_URL = "https://integrate.api.nvidia.com/v1"


def _format_error_detail(e: Exception) -> str:
    """Format an exception with its causal chain for diagnostics."""
    parts = [repr(e)]
    cause = e.__cause__
    while cause is not None:
        parts.append(f"<- {type(cause).__name__}: {cause}")
        cause = cause.__cause__
    return " | ".join(parts)


# ---------------------------------------------------------------------------
# Role error helpers - handle models that reject "system" role
# ---------------------------------------------------------------------------

_system_as_user_cache: set[str] = set()
"""Runtime cache of models known to reject system role. Populated on first
422 error and used by subsequent retry attempts to avoid rebuilding with
the original body (which would cause another 422)."""


def _model_rejects_system(model: str) -> bool:
    """Check if model is known to reject system role."""
    return model in _system_as_user_cache


def _is_role_error(e: Exception) -> bool:
    """Check if an error is about the system role being rejected.

    Matches errors like: "Input should be 'user' or 'assistant'" where
    the location path contains "role" (e.g., loc=['body','messages',1,'role']).
    Also matches errors like: "System message must be at the beginning" (500 errors).
    """
    try:
        # Try to get response from the exception
        response = getattr(e, "response", None)
        if response is not None:
            err_body = response.json()
            # Check for 422 validation errors
            for detail in err_body.get("detail", []):
                loc = detail.get("loc", [])
                msg = detail.get("msg", "")
                if "role" in loc:
                    return True
                if ("user" in msg and "assistant" in msg) or "role" in msg.lower():
                    return True
            # Check for 500 errors with system message placement issue
            error_msg = err_body.get("error", {}).get("message", "")
            if "System message must be at the beginning" in error_msg:
                return True
        # Also check body attribute for different error formats
        body = getattr(e, "body", None)
        if body:
            msg = str(body)
            if "System message must be at the beginning" in msg:
                return True
    except Exception:
        pass
    return False


# ---------------------------------------------------------------------------
# Thinking parameter error helpers - handle models that reject thinking params
# ---------------------------------------------------------------------------

_thinking_unsupported_cache: set[str] = set()
"""Runtime cache of models known to reject thinking/reasoning parameters.
Populated on first 400 error with "Unsupported parameter" and used by
subsequent retry attempts to avoid sending thinking params again."""


def _model_rejects_thinking(model: str) -> bool:
    """Check if model is known to reject thinking/reasoning parameters."""
    return model in _thinking_unsupported_cache


def mark_thinking_unsupported(model: str) -> None:
    """Mark a model as not supporting thinking parameters."""
    if model:
        _thinking_unsupported_cache.add(model)


def _is_thinking_param_error(e: Exception) -> bool:
    """Check if a 400 error is about unsupported thinking/reasoning parameters.

    Matches errors like: "Validation: Unsupported parameter(s): `thinking`, `reasoning_split`, `include_reasoning`, `return_tokens_as_token_ids`"
    """
    try:
        # Check for standard OpenAI error format (BadRequestError, etc.)
        response = getattr(e, "response", None)
        if response is not None:
            err_body = response.json()
            msg = err_body.get("error", {}).get("message", "")
            if "Unsupported parameter" in msg:
                # Check if the unsupported params are thinking-related
                thinking_params = {
                    "thinking",
                    "reasoning_split",
                    "include_reasoning",
                    "return_tokens_as_token_ids",
                    "reasoning_effort",
                }
                for param in thinking_params:
                    if param in msg:
                        return True
        # Check for alternative error format
        body = getattr(e, "body", None)
        if body:
            msg = str(body)
            if "Unsupported parameter" in msg:
                thinking_params = {
                    "thinking",
                    "reasoning_split",
                    "include_reasoning",
                    "return_tokens_as_token_ids",
                    "reasoning_effort",
                }
                for param in thinking_params:
                    if param in msg:
                        return True
    except Exception:
        pass
    return False


def _is_retryable_server_error(e: Exception) -> bool:
    """Check if an error is a 5xx server error that should trigger a retry.

    Matches InternalServerError (500), and APIStatusError/APIError with
    status codes 502, 503, 504 (bad gateway, unavailable, gateway timeout).
    Also matches httpx.HTTPStatusError with those status codes.
    """
    # InternalServerError is always 500
    if isinstance(e, InternalServerError):
        return True

    # Check APIStatusError / APIError for status code
    response = getattr(e, "response", None)
    if response is not None:
        status_code = getattr(response, "status_code", None)
        if status_code in _RETRYABLE_HTTP_STATUS:
            return True

    # Check httpx.HTTPStatusError
    if hasattr(e, "response") and e.response is not None:
        status_code = getattr(e.response, "status_code", None)
        if status_code in _RETRYABLE_HTTP_STATUS:
            return True

    return False


def _rebuild_without_thinking(body: dict) -> dict:
    """Clone request body, removing thinking/reasoning parameters from extra_body."""
    new_body = dict(body)
    if "extra_body" in new_body:
        extra_body = dict(new_body["extra_body"])
        # Remove thinking-related parameters
        thinking_keys = {
            "thinking",
            "reasoning_split",
            "chat_template_kwargs",
            "return_tokens_as_token_ids",
            "reasoning_effort",
            "include_reasoning",
        }
        for key in thinking_keys:
            extra_body.pop(key, None)
        if extra_body:
            new_body["extra_body"] = extra_body
        else:
            new_body.pop("extra_body", None)
    return new_body


def _system_to_user(msg: dict) -> dict:
    """Convert a system message to a user message with a prefix."""
    return {
        "role": "user",
        "content": f"[System Instructions]\n{msg.get('content', '')}",
    }


def _save_model_override(model_name: str) -> None:
    """Auto-create overrides.json with this model entry for future runs."""
    if not model_name:
        return
    if getattr(sys, "frozen", False):
        base = Path(sys.executable).parent
    else:
        base = Path(__file__).resolve().parent.parent
    path = base / "overrides.json"

    overrides: dict = {}
    if path.exists():
        try:
            overrides = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError, OSError:
            overrides = {}

    model_overrides = overrides.setdefault("model_overrides", {})
    if model_name not in model_overrides:
        model_overrides[model_name] = {"system_as_user": True}
        try:
            path.write_text(
                json.dumps(overrides, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            logger.info("Created overrides.json entry for {}", model_name)
        except OSError as e:
            logger.warning("Could not write overrides.json: {}", e)


def _rebuild_with_system_as_user(body: dict) -> dict:
    """Clone request body, replacing system messages with user messages."""
    new_body = dict(body)
    new_body["messages"] = [
        _system_to_user(m) if m.get("role") == "system" else m
        for m in body.get("messages", [])
    ]
    return new_body


class NvidiaNimProvider(BaseProvider):
    """NVIDIA NIM provider using OpenAI-compatible API."""

    def __init__(self, config: ProviderConfig, *, nim_settings: NimSettings):
        super().__init__(config)
        self._provider_name = "NIM"
        self._api_key = config.api_key
        self._base_url = (config.base_url or NVIDIA_NIM_BASE_URL).rstrip("/")
        self._nim_settings = nim_settings
        self._global_rate_limiter = GlobalRateLimiter.get_instance(
            rate_limit=config.rate_limit,
            rate_window=config.rate_window,
            max_concurrency=config.max_concurrency,
            worker_limit=config.request_queue_max_concurrent,
        )

        # Initialize request queue
        self._request_queue: RequestQueue | None = None
        if config.request_queue_enabled:
            self._request_queue = RequestQueue(
                max_concurrent=config.request_queue_max_concurrent,
                max_queue_size=config.request_queue_max_size,
                num_workers=config.request_queue_num_workers,
                queue_timeout=config.request_queue_timeout,
                enabled=config.request_queue_enabled,
            )

        # Create header-capturing transport for rate limit parsing
        capture_store = CapturedHeaders.get_instance()
        custom_transport = HeaderCapturingTransport(capture_store)
        print("🔧 HeaderCapturingTransport initialized for NIM provider", flush=True)

        # Configure HTTP client with custom transport
        http_client = httpx.AsyncClient(transport=custom_transport)

        self._client = AsyncOpenAI(
            api_key=self._api_key,
            base_url=self._base_url,
            max_retries=0,  # Disable built-in retries - we handle retries in our own logic
            timeout=httpx.Timeout(
                config.http_read_timeout,
                connect=config.http_connect_timeout,
                read=config.http_read_timeout,
                write=config.http_write_timeout,
            ),
            http_client=http_client,
        )
        self._header_capture = capture_store

    async def _ensure_queue_started(self) -> None:
        """Lazily start the request queue workers."""
        if self._request_queue and not self._request_queue._workers:
            await self._request_queue.start()

    async def cleanup(self) -> None:
        """Release HTTP client resources and shutdown request queue."""
        # Shutdown request queue first
        if self._request_queue:
            await self._request_queue.shutdown(drain=True, timeout=30.0)

        # Then close HTTP client
        client = getattr(self, "_client", None)
        if client is not None:
            await client.aclose()

    def _execute_queued(self, coro_factory: callable, priority: int = RequestPriority.NORMAL):
        """Execute a request through the queue or directly if disabled.

        Args:
            coro_factory: Callable that returns an awaitable
            priority: Request priority (HIGH, NORMAL, LOW)

        Returns:
            Awaitable that resolves to the request result
        """
        if self._request_queue and self._request_queue.is_enabled:
            return self._request_queue.enqueue(coro_factory, priority)
        else:
            # Queue disabled - execute directly
            return coro_factory()

    def _build_request_body(
        self, request: Any, *, model_override: str | None = None
    ) -> dict:
        """Build request body for NIM API.

        Checks the runtime cache for models known to reject the system role
        so that retry loops don't accidentally send system→model→422 again.
        Also checks for models known to reject thinking/reasoning parameters.

        Args:
            request: The request object
            model_override: Optional model name to override the request's model
        """
        assert self._nim_settings is not None
        body = build_request_body(request, self._nim_settings)

        # Apply model override if provided
        if model_override:
            body["model"] = model_override

        model = body.get("model", "")
        if _model_rejects_system(model):
            body = _rebuild_with_system_as_user(body)
        if _model_rejects_thinking(model):
            body = _rebuild_without_thinking(body)
        return body

    async def buffered_request(
        self,
        request: Any,
        input_tokens: int = 0,
        *,
        request_id: str | None = None,
        model_override: str | None = None,
        priority: int = RequestPriority.NORMAL,
    ) -> dict:
        """Send a non-streaming request and return complete Anthropic-format JSON response.

        With retry_on_truncation set, this will retry on transient errors
        (APIConnectionError, APITimeoutError, and similar dropped connections)
        to handle NVIDIA backend cutouts.

        Requests are queued to respect NVIDIA NIM worker limits.
        """
        await self._ensure_queue_started()

        async def _do_buffered_request():
            return await self._buffered_request_impl(
                request, input_tokens, request_id, model_override
            )

        return await self._execute_queued(_do_buffered_request, priority)

    async def _buffered_request_impl(
        self,
        request: Any,
        input_tokens: int = 0,
        *,
        request_id: str | None = None,
        model_override: str | None = None,
        use_rate_limiter_concurrency: bool = True,
    ) -> dict:
        """Internal implementation of buffered request with retry logic.

        Args:
            use_rate_limiter_concurrency: If True, uses rate limiter's concurrency slot.
                If False, assumes caller already holds a worker slot (e.g., from queue).
        """
        tag = self._provider_name
        max_retries = self._config.retry_on_truncation
        retry_delay = self._config.retry_delay
        last_error = None

        attempt = 0
        while max_retries == 0 or attempt < (max_retries + 1):
            try:
                if attempt > 0:
                    logger.warning(
                        "{}_BUFFERED: Retry attempt {}/{} after {}s delay",
                        tag,
                        attempt,
                        max_retries if max_retries > 0 else "∞",
                        retry_delay * attempt,
                    )
                    await asyncio.sleep(retry_delay * attempt)

                body = self._build_request_body(request, model_override=model_override)
                req_tag = f" request_id={request_id}" if request_id else ""
                logger.info(
                    "{}_BUFFERED: attempt={}{} model={} msgs={} tools={}",
                    tag,
                    attempt + 1,
                    req_tag,
                    body.get("model"),
                    len(body.get("messages", [])),
                    len(body.get("tools", [])),
                )

                # Count every attempt (including retries) against the
                # global rate limit so the real RPM is tracked, not just
                # successful requests.  This prevents silent rate limit
                # overshoot when NVIDIA backend cutouts cause retries.
                await self._global_rate_limiter.wait_if_blocked()

                # Use rate limiter's concurrency slot only if not already
                # inside a queue worker (which holds the queue's worker semaphore)
                if use_rate_limiter_concurrency:
                    async with self._global_rate_limiter.concurrency_slot():
                        return await self._do_buffered_request(body, request, input_tokens, request_id)

                # Queue already holds worker slot - just execute
                return await self._do_buffered_request(body, request, input_tokens, request_id)

            except RateLimitError as e:
                # RateLimitError (429) – trigger adaptive backoff and retry
                last_error = e
                self._global_rate_limiter.on_rate_limit_hit()
                detail = _format_error_detail(e)
                exhaustion_msg = (
                    f"after {attempt + 1} attempts"
                    if max_retries > 0
                    else f"after {attempt + 1} attempts (endless retries - still trying)"
                )
                if max_retries > 0 and attempt >= max_retries:
                    logger.error(
                        "{}_BUFFERED: Non-retryable RateLimitError {} - {}",
                        tag,
                        exhaustion_msg,
                        detail,
                    )
                    raise
                # Exponential backoff with jitter
                base_delay = 2.0
                max_delay = 60.0
                delay = min(base_delay * (2**attempt), max_delay)
                delay += random.uniform(0, 1.0)
                logger.warning(
                    "{}_BUFFERED: RateLimited (429) attempt {}/{} - retrying in {:.1f}s. {}",
                    tag,
                    attempt + 1,
                    max_retries if max_retries > 0 else "∞",
                    delay,
                    detail,
                )
                self._global_rate_limiter.set_blocked(delay)
                await asyncio.sleep(delay)
                attempt += 1
                continue

            except (
                APIConnectionError,
                APITimeoutError,
                InternalServerError,
                APIStatusError,
            ) as e:
                # Check if it's a retryable 5xx error
                is_retryable = isinstance(
                    e, (APIConnectionError, APITimeoutError)
                ) or _is_retryable_server_error(e)
                last_error = e
                detail = _format_error_detail(e)
                exhaustion_msg = (
                    f"after {attempt + 1} attempts"
                    if max_retries > 0
                    else f"after {attempt + 1} attempts (endless retries - still trying)"
                )
                if is_retryable:
                    logger.warning(
                        "{}_BUFFERED: Retryable error {} {} - {}",
                        tag,
                        type(e).__name__,
                        exhaustion_msg,
                        detail,
                    )
                else:
                    logger.error(
                        "{}_BUFFERED: Non-retryable error {} {} - {}",
                        tag,
                        type(e).__name__,
                        exhaustion_msg,
                        detail,
                    )
                if max_retries > 0 and attempt >= max_retries:
                    if is_retryable:
                        if isinstance(e, (APIConnectionError, APITimeoutError)):
                            raise StreamTruncatedError(
                                f"NVIDIA backend dropped connection after "
                                f"{max_retries + 1} attempts: {e}"
                            ) from e
                        raise StreamTruncatedError(
                            f"NVIDIA backend server error (5xx) after "
                            f"{max_retries + 1} attempts: {e}"
                        ) from e
                    raise
                if is_retryable:
                    attempt += 1
                    continue
                # Non-retryable error - raise immediately without counting as a retry attempt
                raise

        # This shouldn't be reached, but just in case
        raise StreamTruncatedError(
            "NVIDIA backend dropped connection after all retries"
        ) from last_error

    async def _do_buffered_request(self, body: dict, request: Any, input_tokens: int, request_id: str | None) -> dict:
        """Execute the actual buffered request (shared by queued and non-queued paths)."""
        req_token = request_id_var.set(request_id)
        try:
            try:
                response = await self._client.chat.completions.create(
                    **body,
                    stream=False,
                )
            except (
                BadRequestError,
                UnprocessableEntityError,
                APIStatusError,
            ) as e:
                if _is_role_error(e):
                    model = body.get("model", "")
                    logger.warning(
                        "{}_BUFFERED: System role rejected by model ({}) - "
                        "retrying with system→user conversion",
                        tag,
                        model,
                    )
                    _system_as_user_cache.add(model)
                    _save_model_override(model)
                    body = _rebuild_with_system_as_user(body)
                    response = await self._client.chat.completions.create(
                        **body,
                        stream=False,
                    )
                elif _is_thinking_param_error(e):
                    model = body.get("model", "")
                    logger.warning(
                        "{}_BUFFERED: Thinking parameters rejected by model ({}) - "
                        "retrying without thinking parameters",
                        tag,
                        model,
                    )
                    mark_thinking_unsupported(model)
                    body = _rebuild_without_thinking(body)
                    response = await self._client.chat.completions.create(
                        **body,
                        stream=False,
                    )
                else:
                    raise
        finally:
            request_id_var.reset(req_token)

        result = self._build_anthropic_response(response, request, input_tokens)
        logger.info(
            "{}_BUFFERED: success attempt={}{}",
            tag,
            attempt + 1,
            req_tag,
        )
        return result

    def _build_anthropic_response(
        self,
        response: Any,
        request: Any,
        input_tokens: int,
    ) -> dict:
        """Convert an OpenAI-compatible completion response into Anthropic format."""
        message_id = f"msg_{uuid.uuid4()}"
        choice = response.choices[0] if response.choices else None
        content = choice.message if choice else None

        # Build content blocks
        content_blocks: list[dict] = []

        if content is not None:
            if content.content:
                content_blocks.append(
                    {
                        "type": "text",
                        "text": content.content,
                    }
                )

            if content.tool_calls:
                for tc in content.tool_calls:
                    tool_block = {
                        "type": "tool_use",
                        "id": tc.id or f"tool_{uuid.uuid4()}",
                        "name": tc.function.name if tc.function else "unknown",
                        "input": {},
                    }
                    if tc.function and tc.function.arguments:
                        try:
                            tool_block["input"] = json.loads(tc.function.arguments)
                        except json.JSONDecodeError:
                            tool_block["input"] = {"raw": tc.function.arguments}
                    content_blocks.append(tool_block)

        if not content_blocks:
            content_blocks.append({"type": "text", "text": ""})

        # Extract usage info
        usage = getattr(response, "usage", None)
        output_tokens = (
            usage.completion_tokens
            if usage and hasattr(usage, "completion_tokens")
            else 0
        )
        provider_input = (
            usage.prompt_tokens
            if usage and hasattr(usage, "prompt_tokens")
            else input_tokens
        )

        # Determine stop reason
        stop_reason = "end_turn"
        if choice and choice.finish_reason:
            stop_reason = map_stop_reason(choice.finish_reason)

        return {
            "id": message_id,
            "type": "message",
            "role": "assistant",
            "content": content_blocks,
            "model": request.model,
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "usage": {
                "input_tokens": provider_input,
                "output_tokens": output_tokens,
            },
        }

    def _process_tool_call(self, tc: dict, sse: SSEBuilder) -> Iterator[str]:
        """Process a single tool call delta and yield SSE events."""
        tc_index = tc.get("index", 0)
        if tc_index < 0:
            tc_index = len(sse.blocks.tool_states)

        fn_delta = tc.get("function", {})
        incoming_name = fn_delta.get("name")
        if incoming_name is not None:
            sse.blocks.register_tool_name(tc_index, incoming_name)

        state = sse.blocks.tool_states.get(tc_index)
        if state is None or not state.started:
            name = state.name if state else ""
            if name or tc.get("id"):
                tool_id = tc.get("id") or f"tool_{uuid.uuid4()}"
                yield sse.start_tool_block(tc_index, tool_id, name)

        args = fn_delta.get("arguments", "")
        if args:
            state = sse.blocks.tool_states.get(tc_index)
            if state is None or not state.started:
                tool_id = tc.get("id") or f"tool_{uuid.uuid4()}"
                name = (state.name if state else None) or "tool_call"
                yield sse.start_tool_block(tc_index, tool_id, name)
                state = sse.blocks.tool_states.get(tc_index)

            current_name = state.name if state else ""
            if current_name == "Task":
                parsed = sse.blocks.buffer_task_args(tc_index, args)
                if parsed is not None:
                    yield sse.emit_tool_delta(tc_index, json.dumps(parsed))
                    return

            yield sse.emit_tool_delta(tc_index, args)

    def _flush_task_arg_buffers(self, sse: SSEBuilder) -> Iterator[str]:
        """Emit buffered Task args as a single JSON delta (best-effort)."""
        for tool_index, out in sse.blocks.flush_task_arg_buffers():
            yield sse.emit_tool_delta(tool_index, out)

    async def stream_response(
        self,
        request: Any,
        input_tokens: int = 0,
        *,
        request_id: str | None = None,
        model_override: str | None = None,
        priority: int = RequestPriority.NORMAL,
    ) -> AsyncIterator[str]:
        """Stream response in Anthropic SSE format.

        Requests are queued to respect NVIDIA NIM worker limits.
        """
        await self._ensure_queue_started()

        async def _stream_factory() -> AsyncIterator[str]:
            async for event in self._stream_response_impl(
                request, input_tokens, request_id, model_override, use_rate_limiter_concurrency=False
            ):
                yield event

        # Queue the streaming request and collect events
        events = await self._request_queue.enqueue_stream(_stream_factory, priority)

        # Yield collected events
        for event in events:
            yield event

    async def _stream_response_impl(
        self,
        request: Any,
        input_tokens: int,
        request_id: str | None,
        model_override: str | None = None,
        use_rate_limiter_concurrency: bool = True,
    ) -> AsyncIterator[str]:
        """Streaming implementation with retry on transient backend disconnections.

        Unlike buffered_request, streaming sends SSE events progressively.
        When the backend drops the connection mid-stream, we retry the entire
        request from scratch rather than forwarding the error to the client
        immediately.  Only after all retries are exhausted do we emit an
        error SSE event to Claude.
        """
        tag = self._provider_name
        max_retries = self._config.retry_on_truncation
        retry_delay = self._config.retry_delay

        body = self._build_request_body(request, model_override=model_override)
        req_tag = f" request_id={request_id}" if request_id else ""
        logger.info(
            "{}_STREAM:{} model={} msgs={} tools={}",
            tag,
            req_tag,
            body.get("model"),
            len(body.get("messages", [])),
            len(body.get("tools", [])),
        )

        # Per-attempt state - always assigned inside the retry loop below,
        # so after the loop `sse`, `think_parser`, and `heuristic_parser`
        # hold the final attempt's state.
        error_occurred = False
        error_message = ""
        finish_reason: str | None = None
        usage_info: Any = None
        last_error_tag = ""

        # Dummy initialization to satisfy the static checker; always
        # overwritten on the first loop iteration before any post-loop read.
        sse = SSEBuilder("", "", 0)
        think_parser = ThinkTagParser()
        heuristic_parser = HeuristicToolParser()

        attempt = 0
        while max_retries == 0 or attempt < (max_retries + 1):
            # Rebuild fresh SSE state for each attempt - the previous stream
            # was truncated so we start a brand-new message.
            message_id = f"msg_{uuid.uuid4()}"
            sse = SSEBuilder(message_id, request.model, input_tokens)
            think_parser = ThinkTagParser()
            heuristic_parser = HeuristicToolParser()
            finish_reason = None
            usage_info = None
            error_occurred = False
            error_message = ""

            if attempt > 0:
                delay = retry_delay * attempt
                logger.info(
                    "{}_STREAM: Retrying {}/{} after {:.1f}s delay (last: {})",
                    tag,
                    attempt + 1,
                    max_retries if max_retries > 0 else "∞",
                    delay,
                    last_error_tag,
                )
                await asyncio.sleep(delay)

            # Emit a fresh message_start on every attempt.  If this is a
            # retry, Claude will see the previous stream end abruptly and
            # then see a new message_start - this signals a clean restart.
            yield sse.message_start()

            # Use rate limiter's concurrency slot only if not already
            # inside a queue worker (which holds the queue's worker semaphore)
            if use_rate_limiter_concurrency:
                async with self._global_rate_limiter.concurrency_slot():
                    async for event in self._do_stream_request(body, sse, think_parser, heuristic_parser, request, attempt, max_retries, req_tag, tag, request_id, finish_reason, usage_info, last_error_tag):
                        yield event
            else:
                # Queue already holds worker slot - just execute
                async for event in self._do_stream_request(body, sse, think_parser, heuristic_parser, request, attempt, max_retries, req_tag, tag, request_id, finish_reason, usage_info, last_error_tag):
                    yield event

            # If we got here without error, the stream completed cleanly - exit retry loop
            if not error_occurred:
                break

        # Detect truncated streams (backend cut out without proper finish_reason)
        has_any_content = (
            sse.accumulated_text or sse.accumulated_reasoning or sse.blocks.tool_states
        )
        if error_occurred and not has_any_content:
            sse.mark_truncated(
                "Backend connection lost before any content was produced"
            )
        elif error_occurred:
            sse.mark_truncated("Backend connection lost mid-stream (partial response)")

        # Flush remaining content
        remaining = think_parser.flush()
        if remaining:
            if remaining.type == ContentType.THINKING:
                for event in sse.ensure_thinking_block():
                    yield event
                yield sse.emit_thinking_delta(remaining.content)
            else:
                for event in sse.ensure_text_block():
                    yield event
                yield sse.emit_text_delta(remaining.content)

        for tool_use in heuristic_parser.flush():
            for event in sse.close_content_blocks():
                yield event

            block_idx = sse.blocks.allocate_index()
            yield sse.content_block_start(
                block_idx,
                "tool_use",
                id=tool_use["id"],
                name=tool_use["name"],
            )
            if tool_use.get("name") == "Task" and isinstance(
                tool_use.get("input"), dict
            ):
                tool_use["input"]["run_in_background"] = False
            yield sse.content_block_delta(
                block_idx,
                "input_json_delta",
                json.dumps(tool_use["input"]),
            )
            yield sse.content_block_stop(block_idx)

        if (
            not error_occurred
            and sse.blocks.text_index == -1
            and not sse.blocks.tool_states
        ):
            for event in sse.ensure_text_block():
                yield event
            yield sse.emit_text_delta(" ")

        for event in self._flush_task_arg_buffers(sse):
            yield event

        for event in sse.close_all_blocks():
            yield event

        output_tokens = (
            usage_info.completion_tokens
            if usage_info and hasattr(usage_info, "completion_tokens")
            else sse.estimate_output_tokens()
        )
        if usage_info and hasattr(usage_info, "prompt_tokens"):
            provider_input = usage_info.prompt_tokens
            if isinstance(provider_input, int):
                logger.debug(
                    "TOKEN_ESTIMATE: our={} provider={} diff={:+d}",
                    input_tokens,
                    provider_input,
                    provider_input - input_tokens,
                )
        # When truncated and no explicit finish_reason, signal truncation clearly
        if sse.truncated and finish_reason is None:
            finish_reason = "length"  # maps to "max_tokens" per STOP_REASON_MAP

        yield sse.message_delta(map_stop_reason(finish_reason), output_tokens)
        yield sse.message_stop()

    async def _do_stream_request(
        self,
        body: dict,
        sse: SSEBuilder,
        think_parser: ThinkTagParser,
        heuristic_parser: HeuristicToolParser,
        request: Any,
        attempt: int,
        max_retries: int,
        req_tag: str,
        tag: str,
        request_id: str | None,
        finish_reason: str | None,
        usage_info: Any,
        last_error_tag: str,
    ) -> AsyncIterator[str]:
        """Execute the actual streaming request (shared by queued and non-queued paths).

        This encapsulates the stream processing logic to avoid code duplication
        between the queue path and the direct execution path.
        """
        error_occurred = False
        error_message = ""

        await self._global_rate_limiter.wait_if_blocked()

        req_token = request_id_var.set(request_id)
        try:
            try:
                stream = await self._global_rate_limiter.execute_with_retry(
                    self._client.chat.completions.create, **body, stream=True
                )
            except (
                BadRequestError,
                UnprocessableEntityError,
                APIStatusError,
            ) as e:
                if _is_role_error(e):
                    model = body.get("model", "")
                    logger.warning(
                        "{}_STREAM: System role rejected by model ({}) - "
                        "retrying with system→user conversion",
                        tag,
                        model,
                    )
                    _system_as_user_cache.add(model)
                    _save_model_override(model)
                    body = _rebuild_with_system_as_user(body)
                    stream = await self._global_rate_limiter.execute_with_retry(
                        self._client.chat.completions.create,
                        **body,
                        stream=True,
                    )
                elif _is_thinking_param_error(e):
                    model = body.get("model", "")
                    logger.warning(
                        "{}_STREAM: Thinking parameters rejected by model ({}) - "
                        "retrying without thinking parameters",
                        tag,
                        model,
                    )
                    mark_thinking_unsupported(model)
                    body = _rebuild_without_thinking(body)
                    stream = await self._global_rate_limiter.execute_with_retry(
                        self._client.chat.completions.create,
                        **body,
                        stream=True,
                    )
                else:
                    raise
            finally:
                request_id_var.reset(req_token)

            try:
                async for chunk in stream:
                    if getattr(chunk, "usage", None):
                        usage_info = chunk.usage

                    if not chunk.choices:
                        continue

                    choice = chunk.choices[0]
                    delta = choice.delta
                    if delta is None:
                        continue

                    if choice.finish_reason:
                        finish_reason = choice.finish_reason
                        logger.debug("{} finish_reason: {}", tag, finish_reason)

                    # Handle reasoning_content (OpenAI extended format)
                    reasoning = getattr(delta, "reasoning_content", None)
                    if reasoning:
                        for event in sse.ensure_thinking_block():
                            yield event
                        yield sse.emit_thinking_delta(reasoning)

                    # Handle text content
                    if delta.content:
                        for part in think_parser.feed(delta.content):
                            if part.type == ContentType.THINKING:
                                for event in sse.ensure_thinking_block():
                                    yield event
                                yield sse.emit_thinking_delta(part.content)
                            else:
                                filtered_text, detected_tools = (
                                    heuristic_parser.feed(part.content)
                                )

                                if filtered_text:
                                    for event in sse.ensure_text_block():
                                        yield event
                                    yield sse.emit_text_delta(filtered_text)

                                for tool_use in detected_tools:
                                    for event in sse.close_content_blocks():
                                        yield event

                                    block_idx = sse.blocks.allocate_index()
                                    if tool_use.get(
                                        "name"
                                    ) == "Task" and isinstance(
                                        tool_use.get("input"), dict
                                    ):
                                        tool_use["input"][
                                            "run_in_background"
                                        ] = False
                                    yield sse.content_block_start(
                                        block_idx,
                                        "tool_use",
                                        id=tool_use["id"],
                                        name=tool_use["name"],
                                    )
                                    yield sse.content_block_delta(
                                        block_idx,
                                        "input_json_delta",
                                        json.dumps(tool_use["input"]),
                                    )
                                    yield sse.content_block_stop(block_idx)

                    # Handle native tool calls
                    if delta.tool_calls:
                        for event in sse.close_content_blocks():
                            yield event
                        for tc in delta.tool_calls:
                            tc_info = {
                                "index": tc.index,
                                "id": tc.id,
                                "function": {
                                    "name": tc.function.name,
                                    "arguments": tc.function.arguments,
                                },
                            }
                            for event in self._process_tool_call(tc_info, sse):
                                yield event

            except APIStatusError as e:
                # Check for system role error during streaming (e.g., "System message must be at the beginning")
                if _is_role_error(e):
                    model = body.get("model", "")
                    logger.warning(
                        "{}_STREAM: System role rejected by model ({}) during streaming - "
                        "retrying with system→user conversion",
                        tag,
                        model,
                    )
                    _system_as_user_cache.add(model)
                    _save_model_override(model)
                    body = _rebuild_with_system_as_user(body)
                    # Increment attempt and retry the whole request
                    attempt += 1
                    raise  # Will be caught by outer retry loop
                # Not a system role error, fall through to generic exception handler
                raise

        except (
            APIConnectionError,
            APITimeoutError,
            httpx.ReadError,
            InternalServerError,
            APIStatusError,
            httpx.HTTPStatusError,
        ) as e:
            # Check if it's a retryable 5xx server error
            is_retryable = isinstance(
                e, (APIConnectionError, APITimeoutError, httpx.ReadError)
            ) or _is_retryable_server_error(e)
            last_error_tag = f"{type(e).__name__}"
            detail = _format_error_detail(e)
            if is_retryable:
                if max_retries == 0 or attempt < max_retries:
                    logger.warning(
                        "{}_STREAM: {} on attempt {}/{} - retrying. {}",
                        tag,
                        type(e).__name__,
                        attempt + 1,
                        max_retries if max_retries > 0 else "∞",
                        detail,
                    )
                    attempt += 1
                    raise  # Will be caught by outer retry loop
                # All retries exhausted for retryable error
                logger.error(
                    "{}_STREAM: {} exhausted after {} attempts: {}",
                    tag,
                    type(e).__name__,
                    attempt + 1,
                    detail,
                )
            else:
                # Non-retryable error - raise immediately without counting as a retry attempt
                logger.error(
                    "{}_STREAM: Non-retryable {} on attempt {}/{}: {}",
                    tag,
                    type(e).__name__,
                    attempt + 1,
                    max_retries if max_retries > 0 else "∞",
                    detail,
                )
                mapped_e = map_error(e)
                error_occurred = True
                error_message = append_request_id(
                    get_user_facing_error_message(
                        mapped_e, read_timeout_s=self._config.http_read_timeout
                    ),
                    request_id,
                )
                logger.info(
                    "{}_STREAM: Emitting SSE error event for {}{}",
                    tag,
                    type(e).__name__,
                    req_tag,
                )
                for event in sse.close_content_blocks():
                    yield event
                for event in sse.emit_error(error_message):
                    yield event
                return
            logger.error(
                "{}_STREAM: {} exhausted after {} attempts: {}",
                tag,
                type(e).__name__,
                attempt + 1,
                detail,
            )
            mapped_e = map_error(e)
            error_occurred = True
            error_message = append_request_id(
                get_user_facing_error_message(
                    mapped_e, read_timeout_s=self._config.http_read_timeout
                ),
                request_id,
            )
            logger.info(
                "{}_STREAM: Emitting SSE error event for {}{}",
                tag,
                type(e).__name__,
                req_tag,
            )
            for event in sse.close_content_blocks():
                yield event
            for event in sse.emit_error(error_message):
                yield event

        except Exception as e:
            # Log exception details for debugging
            logger.error(
                "{}_EXCEPTION_DIAGNOSTIC: type={} bases={} error={}",
                tag,
                type(e).__name__,
                type(e).__bases__,
                e,
            )
            # Non-retryable errors (auth, rate limit, etc.) - surface immediately
            logger.error(
                "{}_ERROR:{} {}: {}", tag, req_tag, type(e).__name__, e
            )
            mapped_e = map_error(e)
            error_occurred = True
            error_message = append_request_id(
                get_user_facing_error_message(
                    mapped_e, read_timeout_s=self._config.http_read_timeout
                ),
                request_id,
            )
            logger.info(
                "{}_STREAM: Emitting SSE error event for {}{}",
                tag,
                type(e).__name__,
                req_tag,
            )
            for event in sse.close_content_blocks():
                yield event
            for event in sse.emit_error(error_message):
                yield event
