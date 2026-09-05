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
import time
import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Iterator
from pathlib import Path

from typing import Any, Awaitable, Callable, TypeVar

T = TypeVar("T")

import httpx
from loguru import logger
from openai import (
    APIConnectionError,
    APIError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    BadRequestError,
    InternalServerError,
    NotFoundError,
    RateLimitError,
    UnprocessableEntityError,
)

# HTTP status codes that should trigger a retry (5xx server errors)
_RETRYABLE_HTTP_STATUS = {500, 502, 503, 504}

from config.nim import NimSettings
from config.settings import get_settings
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
from providers.sse_builder import SSEBuilder, map_stop_reason, sse_content_block_stop
from providers.think_parser import ContentType, ThinkTagParser, split_think_content
from providers.dsml_parser import DsmlParser, is_dsml_model, parse_dsml_tool_calls

NVIDIA_NIM_BASE_URL = "https://integrate.api.nvidia.com/v1"

# Appended to any truncated/partial response delivered to the client so the
# model knows the reply was cut short and can adapt instead of assuming the
# task finished.
_PARTIAL_RESPONSE_NOTE = (
    "\n\n[PROXY NOTICE] The upstream model's connection dropped mid-response, "
    "so this is a partial reply and it may end abruptly. If the task looks "
    "incomplete, split the work into smaller chunks or use a different "
    "approach and continue."
)


def _format_error_detail(e: Exception) -> str:
    """Format an exception with its causal chain for diagnostics.

    Uses ``repr()`` for each link so exceptions with an empty ``str()``
    (e.g. ``httpx.ReadTimeout()``) render a non-empty, typed link instead of
    a bare ``<- ReadTimeout: `` tail. Follows ``__context__`` when there is no
    explicit ``__cause__``, and caps the chain length.
    """
    parts = [repr(e)]
    cause = e.__cause__ if e.__cause__ is not None else e.__context__
    chain = 0
    while cause is not None and chain < 4:
        msg = str(cause).strip()
        if msg:
            parts.append(f"<- {type(cause).__name__}: {msg}")
        else:
            parts.append(f"<- {type(cause).__name__}()")
        cause = cause.__cause__ if cause.__cause__ is not None else cause.__context__
        chain += 1
    if cause is not None:
        parts.append("<- ...")
    return " | ".join(parts)


def _timeout_subtype(e: Exception, *, read: float, connect: float, write: float) -> str:
    """Return which timeout threshold fired, for diagnostics.

    Returns a short, non-empty suffix when ``e`` is one of the httpx timeout
    subclasses (read / connect / write) so the log makes clear which threshold
    was exceeded and what it was configured to.
    """
    if isinstance(e, httpx.ReadTimeout):
        return f" (read timeout; configured {read:g}s)"
    if isinstance(e, httpx.ConnectTimeout):
        return f" (connect timeout; configured {connect:g}s)"
    if isinstance(e, httpx.WriteTimeout):
        return f" (write timeout; configured {write:g}s)"
    return ""


def _req_ctx(request_id: str | None, body: dict | None) -> str:
    """Build a compact request-context string for error logs.

    Returns ``"[request_id=..., model=...]"`` (or ``""``) so retry warnings can
    carry the failing request's identity and model in the same line.
    """
    parts = []
    if request_id:
        parts.append(f"request_id={request_id}")
    model = (body or {}).get("model", "")
    if model:
        parts.append(f"model={model}")
    return f"[{', '.join(parts)}]" if parts else ""


def _fresh_tool_id() -> str:
    """Return a globally-unique Claude-style tool_use id.

    Never forward the backend's raw tool_call id. Kimi-k3 on NVIDIA NIM returns
    ids in ``Name:index`` format (``Bash:0``, ``Read:1``), which repeat for every
    same-named call across turns. Claude Code sanitizes assistant transcripts by
    de-duplicating tool_use blocks on ``id``: a repeated id is treated as a
    duplicate, stripped, and the whole turn is replaced with ``[Tool use
    interrupted]`` — deadlocking the session in an empty re-prompt loop. Always
    minting a fresh unique id (like the real Anthropic API does) avoids that.
    """
    return f"toolU_{uuid.uuid4().hex}"


async def _keepalive(
    gen: AsyncGenerator[str, Any], interval: float = 15.0
) -> AsyncGenerator[str, None]:
    """Wrap a stalled async generator, emitting SSE keepalive comments.

    While the underlying generator produces nothing (e.g. the request queue is
    still buffering a long generation), yield SSE comment lines (`: keepalive`)
    every `interval` seconds so clients don't hit idle/read timeouts. SSE
    comments are ignored by clients per the SSE spec.
    """
    task = asyncio.ensure_future(gen.__anext__())
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=interval)
            if task in done:
                try:
                    yield task.result()
                except StopAsyncIteration:
                    return
                task = asyncio.ensure_future(gen.__anext__())
            else:
                yield ": keepalive\n\n"
    finally:
        if not task.done():
            task.cancel()
        try:
            await task  # let the cancelled __anext__ unwind before closing gen
        except BaseException:
            pass
        # Unblock the inner generator if it's suspended on an await.
        try:
            await gen.aclose()
        except Exception:
            pass


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

# New: Model-specific budget overrides for dynamic reduction on budget_exceeded errors
_thinking_budget_override: dict[str, int] = {}
"""Runtime cache of models with dynamically reduced reasoning budgets.
When a budget_exceeded error occurs, we reduce the budget and retry WITH thinking."""


def _model_rejects_thinking(model: str) -> bool:
    """Check if model is known to reject thinking/reasoning parameters."""
    return model in _thinking_unsupported_cache


def mark_thinking_unsupported(model: str) -> None:
    """Mark a model as not supporting thinking parameters."""
    if model:
        _thinking_unsupported_cache.add(model)


def get_thinking_budget_override(model: str) -> int | None:
    """Get dynamically reduced budget for a model (if any)."""
    return _thinking_budget_override.get(model)


def set_thinking_budget_override(model: str, budget: int) -> None:
    """Set a dynamically reduced budget for a model."""
    if budget > 0:
        _thinking_budget_override[model] = max(1024, budget)  # floor at 1k


_thinking_effort_unsupported_cache: set[str] = set()
"""Runtime cache of models that accept enable_thinking but reject *_effort flags.
Populated on first 'Unsupported parameter: *_effort' error; future buildings drop the flags."""


def _model_rejects_effort_flags(model: str) -> bool:
    """Check if model is known to reject *_effort flags (but may support enable_thinking)."""
    return model in _thinking_effort_unsupported_cache


def mark_effort_flags_unsupported(model: str) -> None:
    """Mark a model as rejecting *_effort flags."""
    if model:
        _thinking_effort_unsupported_cache.add(model)


def clear_thinking_caches(model: str | None = None) -> None:
    """Clear thinking-related caches for a specific model or all models."""
    if model:
        _thinking_unsupported_cache.discard(model)
        _thinking_budget_override.pop(model, None)
        _thinking_effort_unsupported_cache.discard(model)
    else:
        _thinking_unsupported_cache.clear()
        _thinking_budget_override.clear()
        _thinking_effort_unsupported_cache.clear()


def _has_thinking_params(body: dict) -> bool:
    """Check if the request body contains thinking/reasoning parameters."""
    extra_body = body.get("extra_body", {})
    thinking_keys = {
        "thinking",
        "reasoning_split",
        "include_reasoning",
        "return_tokens_as_token_ids",
        "reasoning_effort",
        "chat_template_kwargs",
    }
    return any(key in extra_body for key in thinking_keys)


def _is_thinking_param_error(e: Exception) -> tuple[bool, str | None, str | None]:
    """Check if a 400 error is about unsupported thinking/reasoning parameters.

    Returns:
        (is_thinking_error, error_type, detail)
        error_type: "unsupported" | "budget_exceeded" | "invalid_param"
        detail: param name (for invalid_param) or budget value (for budget_exceeded)

    Matches errors like:
    - "Validation: Unsupported parameter(s): `thinking`, `reasoning_split`" -> unsupported
    - "reasoning_budget 50000 exceeds maximum allowed 32768" -> budget_exceeded
    - "Invalid value for `reasoning_budget`" -> invalid_param
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
                    "reasoning_budget",  # NVIDIA returns this when model doesn't support reasoning_budget param
                }
                for param in thinking_params:
                    if param in msg:
                        return True, "unsupported", param
            # Effort flags: model rejects *_effort but may still accept enable_thinking
            effort_params = {"low_effort", "medium_effort", "high_effort"}
            for param in effort_params:
                if param in msg:
                    return True, "effort_unsupported", param
            # Check for budget exceeded
            if "reasoning_budget" in msg and "exceed" in msg.lower():
                # Try to extract the max allowed budget from error
                import re
                match = re.search(r"maximum\s+(?:allowed\s+)?(\d+)", msg, re.IGNORECASE)
                if not match:
                    match = re.search(r"must be.*?(\d+)", msg, re.IGNORECASE)
                detail = match.group(1) if match else "budget_exceeded"
                return True, "budget_exceeded", detail
            # Check for invalid value
            if "Invalid value" in msg and "reasoning_budget" in msg:
                return True, "invalid_param", "reasoning_budget"
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
                    "reasoning_budget",  # NVIDIA returns this when model doesn't support reasoning_budget param
                }
                for param in thinking_params:
                    if param in msg:
                        return True, "unsupported", param
            effort_params = {"low_effort", "medium_effort", "high_effort"}
            for param in effort_params:
                if param in msg:
                    return True, "effort_unsupported", param
            if "reasoning_budget" in msg and "exceed" in msg.lower():
                import re
                match = re.search(r"maximum\s+(?:allowed\s+)?(\d+)", msg, re.IGNORECASE)
                detail = match.group(1) if match else "budget_exceeded"
                return True, "budget_exceeded", detail
            if "Invalid value" in msg and "reasoning_budget" in msg:
                return True, "invalid_param", "reasoning_budget"
    except Exception:
        pass
    return False, None, None




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
    response = getattr(e, "response", None)
    if response is not None:
        status_code = getattr(response, "status_code", None)
        if status_code in _RETRYABLE_HTTP_STATUS:
            return True

    return False


def _is_resource_exhausted_error(e: Exception) -> bool:
    """Check if an error indicates NVIDIA worker limit exhaustion.

    NVIDIA returns ResourceExhausted errors as APIError in 200 responses
    (not HTTP status codes), so they bypass normal retry logic.
    """
    msg = str(e).lower()
    return "resourceexhausted" in msg or "worker local total request limit" in msg


# ============================================================
# Shared retry logic for buffered and streaming requests
# ============================================================

async def _execute_with_retry(
    coro_factory: Callable[[], Awaitable[T]],
    max_retries: int,
    resource_exhausted_retries: int,
    retry_delay: float,
    tag: str,
    is_retryable_fn: Callable[[Exception], bool],
    is_resource_exhausted_fn: Callable[[Exception], bool],
    on_rate_limited: Callable[[], None],
    on_success: Callable[[], None],
    should_fallback_system_role_fn: Callable[[Exception], tuple[bool, dict | None]] | None = None,
    should_fallback_thinking_fn: Callable[[Exception], tuple[bool, dict | None]] | None = None,
    rebuild_request_fn: Callable[[], dict] | None = None,
    body: dict | None = None,
    request_id: str | None = None,
) -> T:
    """
    Shared retry logic for buffered and streaming requests.

    Args:
        coro_factory: Factory function that creates the coroutine to execute
        max_retries: Base max retries (0 = endless)
        resource_exhausted_retries: Specific retry count for ResourceExhausted errors
        retry_delay: Base delay between retries (multiplied by attempt)
        tag: Logging tag (e.g., "NVIDIA")
        is_retryable_fn: Callable to determine if error is retryable
        is_resource_exhausted_fn: Callable to detect ResourceExhausted errors
        on_rate_limited: Callback when 429 hit
        on_success: Callback after successful request (no 429)
        should_fallback_system_role_fn: Optional callable to detect system role errors
            Returns (should_retry, new_body) where new_body is the modified request body
        should_fallback_thinking_fn: Optional callable to detect thinking param errors
            Returns (should_retry, new_body) where new_body is the modified request body
        rebuild_request_fn: Optional factory to rebuild request body after fallback
        body: Request body (for logging)
        request_id: Optional request ID for logging

    Returns:
        Result of successful coro_factory() execution
    """
    attempt = 0
    max_retries_effective = max_retries
    last_error = None
    body_ref = body  # Mutable reference for body rebuilds

    while max_retries == 0 or attempt < (max_retries + 1):
        try:
            if attempt > 0:
                delay = retry_delay * attempt
                logger.warning(
                    "{}: Retry attempt {}/{} after {:.1f}s delay",
                    tag,
                    attempt + 1,
                    max_retries if max_retries > 0 else "∞",
                    retry_delay * attempt,
                )
                await asyncio.sleep(delay)

            # Execute the coroutine
            result = await coro_factory()
            # Track success for auto-restore
            try:
                on_success()
            except (AttributeError, TypeError):
                pass  # on_success may not be provided
            return result

        except RateLimitError as e:
            # RateLimitError (429) - track rate limit hit
            max_retries_effective = max_retries
            on_rate_limited()
            detail = _format_error_detail(e)

            if max_retries > 0 and attempt >= max_retries:
                logger.error(
                    "{}: Non-retryable RateLimitError after {} attempts - {}",
                    tag,
                    attempt + 1,
                    _format_error_detail(e),
                )
                raise

            # Exponential backoff with jitter for 429
            base_delay = 2.0
            max_delay = 60.0
            delay = min(base_delay * (2**attempt), max_delay)
            delay += random.uniform(0, 1.0)
            detail = _format_error_detail(e)
            logger.warning(
                "{}: RateLimited (429) attempt {}/{} - retrying in {:.1f}s. {}",
                tag,
                attempt + 1,
                max_retries if max_retries > 0 else "∞",
                delay,
                detail,
            )
            attempt += 1
            continue

        except Exception as e:
            # Check if it's a system role fallback error
            if should_fallback_system_role_fn and should_fallback_system_role_fn(e)[0]:
                # System role fallbacks don't count against retry budget
                if rebuild_request_fn is not None:
                    body_ref = rebuild_request_fn()
                raise  # Will be caught by outer loop and retried

            # Check if it's a thinking param fallback error
            if should_fallback_thinking_fn and should_fallback_thinking_fn(e)[0]:
                should_retry, new_body = should_fallback_thinking_fn(e)
                if should_retry and rebuild_request_fn is not None:
                    body_ref = rebuild_request_fn()
                raise  # Will be caught by outer loop and retried

            # Check if it's a ResourceExhausted error (has its own retry count)
            is_resource_exhausted = is_resource_exhausted_fn(e)
            is_retryable = is_retryable_fn(e) or is_resource_exhausted

            if is_resource_exhausted:
                effective_max_retries = resource_exhausted_retries
            else:
                effective_max_retries = max_retries

            if is_retryable:
                exhaustion_msg = (
                    f"after {attempt + 1} attempts"
                    if effective_max_retries > 0
                    else f"after {attempt + 1} attempts (endless retries - still trying)"
                )
                if effective_max_retries > 0 and attempt >= effective_max_retries:
                    logger.error(
                        "{}: {} exhausted after {} attempts: {}",
                        tag,
                        type(e).__name__,
                        attempt + 1,
                        _format_error_detail(e),
                    )
                    raise
                # Exponential backoff with jitter for retryable errors
                base_delay = 2.0
                max_delay = 60.0
                delay = min(base_delay * (2**attempt), max_delay)
                delay += random.uniform(0, 1.0)
                detail = _format_error_detail(e)
                logger.warning(
                    "{}: {} on attempt {}/{} - retrying in {:.1f}s. {}",
                    tag,
                    type(e).__name__,
                    attempt + 1,
                    max_retries if max_retries > 0 else "∞",
                    delay,
                    detail,
                )
                attempt += 1
                continue
            else:
                # Non-retryable error - raise immediately
                raise

    raise StreamTruncatedError("Exhausted all retries") from last_error


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


def _rebuild_without_effort_flags(body: dict) -> dict:
    """Clone body, removing only *_effort flags from extra_body.chat_template_kwargs.

    Keeps enable_thinking (and any other chat_template_kwargs keys). For models
    that accept enable_thinking but reject low/medium/high_effort flags.
    """
    new_body = dict(body)
    extra_body = dict(new_body.get("extra_body", {}))
    ctk = extra_body.get("chat_template_kwargs")
    if isinstance(ctk, dict):
        ctk = dict(ctk)
        for k in ("low_effort", "medium_effort", "high_effort"):
            ctk.pop(k, None)
        if ctk:
            extra_body["chat_template_kwargs"] = ctk
        else:
            extra_body.pop("chat_template_kwargs", None)
        if extra_body:
            new_body["extra_body"] = extra_body
        else:
            new_body.pop("extra_body", None)
    return new_body


def _rebuild_with_reduced_budget(body: dict, new_budget: int) -> dict:
    """Clone request body, updating reasoning_budget in extra_body to a reduced value.

    Keeps all other thinking/reasoning parameters intact.
    """
    new_body = dict(body)
    if "extra_body" in new_body:
        extra_body = dict(new_body["extra_body"])
        extra_body["reasoning_budget"] = max(1024, new_budget)  # floor at 1k
        new_body["extra_body"] = extra_body
    return new_body


def _system_to_user(msg: dict) -> dict:
    """Convert a system message to a user message with a prefix."""
    return {
        "role": "user",
        "content": f"[System Instructions]\n{msg.get('content', '')}",
    }


def _rebuild_without_key(body: dict, key_to_remove: str) -> dict:
    """Clone request body, removing a specific key from extra_body."""
    new_body = dict(body)
    if "extra_body" in new_body:
        extra_body = dict(new_body["extra_body"])
        extra_body.pop(key_to_remove, None)
        if extra_body:
            new_body["extra_body"] = extra_body
        else:
            new_body.pop("extra_body", None)
    return new_body


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
        except (json.JSONDecodeError, OSError):
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
            rpm_reset=config.rpm_reset,
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
        logger.info("HeaderCapturingTransport initialized for NIM provider")

        # Configure HTTP client with custom transport
        http_client = httpx.AsyncClient(transport=custom_transport)

        self._client = AsyncOpenAI(
            api_key=self._api_key,
            base_url=self._base_url,
            max_retries=0,  # Disable built-in retries - we handle retries in our own logic
            timeout=httpx.Timeout(
                config.http_read_timeout,
                connect=config.http_connect_timeout,
                # Bounded idle-read timeout. `None` would let a stalled stream
                # (NVIDIA returns 200 + `nvcf-status: fulfilled` but then sends
                # zero chunks) block forever — the client sees a half-open
                # response and fails with "empty or malformed response (HTTP 200)".
                # A bounded read timeout raises httpx.ReadTimeout, which the
                # stream retry path classifies as retryable.
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

        # Then close HTTP client (AsyncOpenAI exposes close(), not aclose())
        client = getattr(self, "_client", None)
        if client is not None:
            close = getattr(client, "close", None)
            if close is not None:
                await close()

    def _execute_queued(self, coro_factory: Callable[..., Awaitable[Any]], priority: int = RequestPriority.NORMAL):
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
        # Extract session_id from request if available
        session_id = getattr(request, 'session_id', None)
        # Pass model_override to build_request_body so thinking params match the swapped model
        body = build_request_body(request, self._nim_settings, session_id=session_id, model_override=model_override)

        model = body.get("model", "")
        if _model_rejects_system(model):
            body = _rebuild_with_system_as_user(body)
        if _model_rejects_thinking(model):
            body = _rebuild_without_thinking(body)
        if _model_rejects_effort_flags(model):
            body = _rebuild_without_effort_flags(body)
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
                request, input_tokens, request_id=request_id, model_override=model_override
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
        body: dict | None = None  # assigned in loop; None-guarded for error logging
        total_start = time.monotonic()

        attempt = 0
        while max_retries == 0 or attempt < (max_retries + 1):
            try:
                if attempt > 0:
                    ctx = _req_ctx(request_id, body)
                    logger.warning(
                        "{}_BUFFERED: Retry attempt {}/{} after {}s delay{}",
                        tag,
                        attempt,
                        max_retries if max_retries > 0 else "∞",
                        retry_delay * attempt,
                        f" {ctx}" if ctx else "",
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
                        result = await self._do_buffered_request(body, request, input_tokens, request_id, tag=tag, attempt=attempt, req_tag=req_tag)
                        self._global_rate_limiter.on_success()
                        return result

                # Queue already holds worker slot - just execute
                result = await self._do_buffered_request(body, request, input_tokens, request_id, tag=tag, attempt=attempt, req_tag=req_tag)
                self._global_rate_limiter.on_success()
                return result

            except RateLimitError as e:
                # RateLimitError (429) – track rate limit hit
                last_error = e
                self._global_rate_limiter.on_rate_limited()
                detail = (
                    _format_error_detail(e)
                    + (f" {_req_ctx(request_id, body)}" if _req_ctx(request_id, body) else "")
                )
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
                httpx.ReadError,
                httpx.TimeoutException,
                InternalServerError,
                APIStatusError,
                APIError,
                NotFoundError,
                # "peer closed connection without sending complete message body
                # (incomplete chunked read)" — NVIDIA dropping mid-body. Not a
                # ReadError subclass (sibling under TransportError), so it must
                # be listed explicitly.
                httpx.RemoteProtocolError,
            ) as e:
                # Check if it's a retryable 5xx error
                is_retryable = isinstance(
                    e, (APIConnectionError, APITimeoutError, httpx.ReadError, httpx.TimeoutException, NotFoundError, httpx.RemoteProtocolError)
                ) or _is_retryable_server_error(e) or _is_resource_exhausted_error(e) or ("service temporarily overloaded" in str(e).lower())
                last_error = e
                ctx = _req_ctx(request_id, body)
                detail = (
                    _format_error_detail(e)
                    + _timeout_subtype(
                        e,
                        read=self._config.http_read_timeout,
                        connect=self._config.http_connect_timeout,
                        write=self._config.http_write_timeout,
                    )
                    + (f" {ctx}" if ctx else "")
                )

                # ResourceExhausted errors have their own retry count
                if _is_resource_exhausted_error(e):
                    effective_max_retries = self._config.resource_exhausted_retries
                else:
                    effective_max_retries = max_retries

                exhaustion_msg = (
                    f"after {attempt + 1} attempts"
                    if effective_max_retries > 0
                    else f"after {attempt + 1} attempts (endless retries - still trying)"
                )
                if is_retryable:
                    # Release the rate limit slot since the request never reached the server
                    await self._global_rate_limiter.release_last_slot()
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
                if effective_max_retries > 0 and attempt >= effective_max_retries:
                    if is_retryable:
                        if isinstance(e, (APIConnectionError, APITimeoutError, httpx.ReadError, httpx.TimeoutException)):
                            raise StreamTruncatedError(
                                f"NVIDIA backend dropped connection after "
                                f"{effective_max_retries + 1} attempts: {e}"
                            ) from e
                        raise StreamTruncatedError(
                            f"NVIDIA backend server error (5xx) after "
                            f"{effective_max_retries + 1} attempts: {e}"
                        ) from e
                    raise
                if is_retryable:
                    # Periodic "still retrying" summary so endless-retry mode is
                    # self-documenting instead of looking like a hang.
                    if attempt > 0 and (
                        (time.monotonic() - total_start) >= 60 or attempt % 5 == 0
                    ):
                        logger.info(
                            "{}_BUFFERED: still retrying {}{} attempt={} total_elapsed={:.0f}s "
                            "last_error={} - proxy not hung, upstream timing out",
                            tag,
                            type(e).__name__,
                            _req_ctx(request_id, body),
                            attempt + 1,
                            time.monotonic() - total_start,
                            _timeout_subtype(
                                e,
                                read=self._config.http_read_timeout,
                                connect=self._config.http_connect_timeout,
                                write=self._config.http_write_timeout,
                            ),
                        )
                    attempt += 1
                    continue
                # Non-retryable error - raise immediately without counting as a retry attempt
                raise

        # This shouldn't be reached, but just in case
        raise StreamTruncatedError(
            "NVIDIA backend dropped connection after all retries"
        ) from last_error

    async def _do_buffered_request(self, body: dict, request: Any, input_tokens: int, request_id: str | None, tag: str = "", attempt: int = 0, req_tag: str = "") -> dict:
        """Execute the actual buffered request (shared by queued and non-queued paths)."""
        req_token = request_id_var.set(request_id)
        try:
            # Acquire worker slot for the entire buffered request duration
            # to respect NVIDIA NIM's per-worker concurrent request limit.
            async with self._global_rate_limiter.worker_slot():
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
                    else:
                        # Smart thinking param error handling
                        is_thinking, error_type, detail = _is_thinking_param_error(e)
                        if is_thinking:
                            model = body.get("model", "")
                            if error_type == "budget_exceeded":
                                # Reduce budget and retry WITH thinking
                                new_budget = int(detail) if detail and detail.isdigit() else 1024
                                if new_budget is not None:
                                    set_thinking_budget_override(model, new_budget)
                                logger.warning(
                                    "{}_BUFFERED: Budget exceeded for model ({}) - "
                                    "reducing reasoning_budget to {} and retrying WITH thinking",
                                    tag,
                                    model,
                                    new_budget,
                                )
                                if new_budget is not None:
                                    body = _rebuild_with_reduced_budget(body, new_budget)
                                response = await self._client.chat.completions.create(
                                    **body,
                                    stream=False,
                                )
                            elif error_type == "invalid_param":
                                # Remove just the invalid param and retry WITH thinking
                                if detail is not None:
                                    logger.warning(
                                        "{}_BUFFERED: Invalid param {} for model ({}) - "
                                        "removing param and retrying WITH thinking",
                                        tag,
                                        detail,
                                        model,
                                    )
                                    body = _rebuild_without_key(body, detail)
                                response = await self._client.chat.completions.create(
                                    **body,
                                    stream=False,
                                )
                            elif error_type == "effort_unsupported":
                                # Model rejects *_effort but accepts enable_thinking:
                                # drop only the effort flags and retry WITH thinking
                                logger.warning(
                                    "{}_BUFFERED: Effort flag {} rejected by model ({}) - "
                                    "retrying with enable_thinking only",
                                    tag,
                                    detail,
                                    model,
                                )
                                mark_effort_flags_unsupported(model)
                                body = _rebuild_without_effort_flags(body)
                                response = await self._client.chat.completions.create(
                                    **body,
                                    stream=False,
                                )
                            else:
                                # Generic "thinking not supported" → full fallback
                                logger.warning(
                                    "{}_BUFFERED: Thinking parameters rejected by model ({}) - "
                                    "retrying WITHOUT thinking parameters",
                                    tag,
                                    model,
                                )
                                mark_thinking_unsupported(model)
                                body = _rebuild_without_thinking(body)
                                response = await self._client.chat.completions.create(
                                    **body,
                                    stream=False,
                                )
                        elif _has_thinking_params(body) and isinstance(e, InternalServerError):
                            # Model doesn't support thinking params but returns 500 instead of 400
                            model = body.get("model", "")
                            logger.warning(
                                "{}_BUFFERED: Internal server error with thinking params on model ({}) - "
                                "retrying WITHOUT thinking parameters",
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

        if self._config.show_nvidia_reply:
            # Mirror the raw NIM reply to the console so the operator can watch
            # the model think in buffer mode (frozen activity = it's stuck).
            choice = response.choices[0] if response.choices else None
            msg = choice.message if choice else None
            if msg is not None:
                self._live_nim_reply(getattr(msg, "reasoning_content", None), kind="THINKING")
                self._live_nim_reply(msg.content, kind="REPLY")

        result = self._build_anthropic_response(response, request, input_tokens, body.get("model", ""))
        logger.info(
            "{}_BUFFERED: success attempt={}{}",
            tag,
            attempt + 1,
            req_tag,
        )
        return result

    def _live_nim_reply(self, text: Any, *, kind: str) -> None:
        """Echo the raw NVIDIA NIM reply to the console when SHOW_NIM_REPLY is on.

        kind is "THINKING" for reasoning_content, "REPLY" for generated text.
        No-op unless the toggle is set (or text is empty).
        """
        if self._config.show_nvidia_reply and text:
            logger.info("NIM_{} | {}", kind, text)

    def _try_parse_dsml_tool_calls(self, content_text: str, model: str) -> tuple[str, list[dict]]:
        """Try to parse DSML tool calls from content text for DeepSeek-V4 models.

        Returns:
            tuple of (remaining_text, parsed_tool_calls)
            If no DSML found or not applicable model, returns (content_text, [])
        """
        if not is_dsml_model(model):
            return content_text, []

        remaining_text, parsed_tool_calls = parse_dsml_tool_calls(content_text)
        if parsed_tool_calls:
            logger.info(
                "Successfully parsed {} DSML tool calls from content for model {}",
                len(parsed_tool_calls), model
            )
        elif remaining_text != content_text:
            # Residual DSML markup (e.g. a dangling closing-tag tail the
            # backend's tool parser failed to consume) was stripped
            logger.warning(
                "Stripped residual DSML markup from content for model {}", model
            )
        else:
            logger.debug("DSML tool_calls not found in content for model {}", model)

        return remaining_text, parsed_tool_calls

    def _build_anthropic_response(
        self,
        response: Any,
        request: Any,
        input_tokens: int,
        model_used: str = "",
    ) -> dict:
        """Convert an OpenAI-compatible completion response into Anthropic format."""
        message_id = f"msg_{uuid.uuid4()}"
        choice = response.choices[0] if response.choices else None
        content = choice.message if choice else None

        # Build content blocks
        content_blocks: list[dict] = []

        if content is not None:
            # DeepSeek-V4 on NIM returns reasoning inline in content (with a
            # trailing </think>) instead of a reasoning_content field;
            # convert it to a proper thinking block before anything else.
            thinking_text, raw_content = split_think_content(
                content.content if content.content else ""
            )
            if thinking_text:
                logger.info(
                    "Converted {} chars of inline <think> reasoning to a thinking block",
                    len(thinking_text),
                )
                content_blocks.append({"type": "thinking", "thinking": thinking_text})

            # Check for DSML tool calls in content (DeepSeek-V4-Pro specific)
            remaining_text, dsml_tool_calls = self._try_parse_dsml_tool_calls(
                raw_content,
                model_used
            )

            # Add text content if any remains
            if remaining_text:
                content_blocks.append(
                    {
                        "type": "text",
                        "text": remaining_text,
                    }
                )

            # Add parsed DSML tool calls
            for tc in dsml_tool_calls:
                content_blocks.append(tc)

            # Add standard tool_calls if present (for other models)
            if content.tool_calls:
                for tc in content.tool_calls:
                    tool_block = {
                        "type": "tool_use",
                        "id": _fresh_tool_id(),
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

        tool_blocks_present = any(
            block.get("type") == "tool_use" for block in content_blocks
        )
        # Guard: never claim tool_use without an actual tool_use block.
        # DeepSeek-V4 sometimes emits DSML tool calls the backend parser fails
        # to extract, so finish_reason says "tool_calls" but no structured
        # call survives. Claude Code hard-fails on tool_use with no tool block.
        if stop_reason == "tool_use" and not tool_blocks_present:
            logger.warning(
                "finish_reason={} but no tool_use block produced; "
                "downgrading stop_reason to end_turn",
                choice.finish_reason if choice else None,
            )
            stop_reason = "end_turn"
        # Inverse guard: DeepSeek emits tool calls as DSML markup, so OpenAI's
        # finish_reason is "stop" even when tool_use blocks were parsed out of
        # the content. Claude Code expects stop_reason="tool_use" when tool_use
        # content blocks are present; end_turn + tool_use blocks is rejected.
        elif tool_blocks_present and stop_reason != "tool_use":
            logger.debug(
                "finish_reason={} but tool_use blocks were produced; "
                "setting stop_reason to tool_use",
                choice.finish_reason if choice else None,
            )
            stop_reason = "tool_use"

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
                tool_id = _fresh_tool_id()
                yield sse.start_tool_block(tc_index, tool_id, name)

        args = fn_delta.get("arguments", "")
        if args:
            state = sse.blocks.tool_states.get(tc_index)
            if state is None or not state.started:
                tool_id = _fresh_tool_id()
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

        Requests are queued to respect NVIDIA NIM worker limits. Events flow
        to the client in real time (not buffered), so Claude sees the first
        token quickly even on long generations.
        """
        await self._ensure_queue_started()

        async def _stream_factory() -> AsyncIterator[str]:
            async for event in self._stream_response_impl(
                request, input_tokens, request_id=request_id, model_override=model_override, use_rate_limiter_concurrency=False
            ):
                yield event

        if self._request_queue is None:
            raise RuntimeError("Request queue is not initialized")

        # Inline dedup: when _stream_response_impl retries, it emits a fresh
        # message_start. Track opened blocks per attempt so we can close them
        # cleanly when a retry happens.
        attempt = 0
        opened_blocks: list[tuple[int, str]] = []  # (index, type) in order

        def _make_close_events() -> Iterator[str]:
            """Emit content_block_stop for each opened block (reverse order)."""
            for block_index, block_type in reversed(opened_blocks):
                yield sse_content_block_stop(block_index)

        async for event in self._request_queue.enqueue_stream(_stream_factory, priority):
            # Detect block open/close events to track state
            if "content_block_start" in event:
                # Extract index and type from the event
                try:
                    data = json.loads(event.split("data: ", 1)[-1])
                    block = data.get("content_block", {})
                    opened_blocks.append((data.get("index", -1), block.get("type", "text")))
                except (json.JSONDecodeError, IndexError):
                    pass
            elif "content_block_stop" in event:
                try:
                    data = json.loads(event.split("data: ", 1)[-1])
                    idx = data.get("index", -1)
                    # Remove from opened_blocks
                    opened_blocks[:] = [(i, t) for i, t in opened_blocks if i != idx]
                except (json.JSONDecodeError, IndexError):
                    pass
            elif "message_start" in event:
                attempt += 1
                if attempt > 1 and opened_blocks:
                    for close_event in _make_close_events():
                        yield close_event
                    opened_blocks.clear()
                    logger.warning(
                        "{}_STREAM: retry attempt {} started; closed {} blocks "
                        "from dropped attempt",
                        self._provider_name,
                        attempt,
                        len(opened_blocks),
                    )
            yield event

    @staticmethod
    def sse_content_block_stop(index: int) -> str:
        """Generate a content_block_stop SSE event."""
        return (
            "event: content_block_stop\n"
            f'data: {json.dumps({"type": "content_block_stop", "index": index})}\n\n'
        )

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
        dsml_parser = DsmlParser()

        attempt = 0
        while max_retries == 0 or attempt < (max_retries + 1):
            # Rebuild fresh SSE state for each attempt - the previous stream
            # was truncated so we start a brand-new message.
            message_id = f"msg_{uuid.uuid4()}"
            sse = SSEBuilder(message_id, request.model, input_tokens)
            think_parser = ThinkTagParser()
            heuristic_parser = HeuristicToolParser()
            dsml_parser = DsmlParser()
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

            # If thinking is enabled, start a thinking block at the beginning if no block has been started yet.
            settings = get_settings()
            if settings.nim.thinking and not sse.blocks.thinking_started and not sse.blocks.text_started:
                yield sse.start_thinking_block()

            # Use rate limiter's concurrency slot only if not already
            # inside a queue worker (which holds the queue's worker semaphore)
            try:
                if use_rate_limiter_concurrency:
                    async with self._global_rate_limiter.concurrency_slot():
                        async for event in self._do_stream_request(body, sse, think_parser, heuristic_parser, dsml_parser, request, attempt, max_retries, req_tag, tag, request_id, finish_reason, usage_info, last_error_tag):
                            yield event
                else:
                    # Queue already holds worker slot - just execute
                    async for event in self._do_stream_request(body, sse, think_parser, heuristic_parser, dsml_parser, request, attempt, max_retries, req_tag, tag, request_id, finish_reason, usage_info, last_error_tag):
                        yield event
            except Exception as e:
                # The exception from _do_stream_request triggers the outer retry loop
                error_occurred = True
                logger.warning(
                    "{}_STREAM: Exception on attempt {}/{} - {}, retrying",
                    tag,
                    attempt + 1,
                    max_retries if max_retries > 0 else "∞",
                    type(e).__name__,
                )
                # Increment the outer attempt counter so the while-condition can
                # eventually terminate a persistently-failing stream and so the
                # backoff delay (retry_delay * attempt) actually ramps up. Without
                # this, attempt stays 0 forever and a broken stream retries
                # endlessly with no delay.
                attempt += 1
                continue

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

        # Flush any remaining DSML tool calls
        for tool_use in dsml_parser.flush():
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

        # Truncated streams (partial delivery or retries exhausted): append a
        # visible notice as the last text so Claude knows the reply was cut
        # short and can split the work or change approach.
        if sse.truncated:
            for event in sse.ensure_text_block():
                yield event
            yield sse.emit_text_delta(_PARTIAL_RESPONSE_NOTE)

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

        stop_reason = map_stop_reason(finish_reason)
        tool_blocks_streamed = (
            sse.blocks.tool_blocks_started > 0
            or any(state.started for state in sse.blocks.tool_states.values())
        )
        # Guard: never claim tool_use without a streamed tool_use block
        # (DeepSeek-V4 DSML tool calls the backend failed to parse, etc.)
        if stop_reason == "tool_use" and not tool_blocks_streamed:
            logger.warning(
                "finish_reason={} but no tool_use block was streamed; "
                "downgrading stop_reason to end_turn",
                finish_reason,
            )
            stop_reason = "end_turn"
        # Inverse guard: if tool_use blocks were streamed but finish_reason
        # was "stop" (DeepSeek emits tool calls as DSML markup, so OpenAI's
        # finish_reason is not "tool_calls"), Claude Code still expects
        # stop_reason="tool_use" when tool_use content blocks are present.
        # Delivering end_turn + tool_use blocks is rejected as malformed.
        elif tool_blocks_streamed:
            logger.debug(
                "finish_reason={} but tool_use blocks were streamed; "
                "setting stop_reason to tool_use",
                finish_reason,
            )
            stop_reason = "tool_use"

        yield sse.message_delta(stop_reason, output_tokens)
        yield sse.message_stop()

    async def _do_stream_request(
        self,
        body: dict,
        sse: SSEBuilder,
        think_parser: ThinkTagParser,
        heuristic_parser: HeuristicToolParser,
        dsml_parser: DsmlParser,
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

        # Acquire worker slot for the entire stream lifecycle (POST + chunk iteration)
        # to respect NVIDIA NIM's per-worker concurrent request limit.
        # execute_with_retry is called with use_worker_slot=False since we
        # hold the slot here for the entire duration.
        async with self._global_rate_limiter.worker_slot():

            req_token = request_id_var.set(request_id)
            try:
                try:
                    stream = await self._global_rate_limiter.execute_with_retry(
                        self._client.chat.completions.create,
                        **body,
                        stream=True,
                        use_worker_slot=False,
                        max_retries=self._config.retry_on_truncation,
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
                            use_worker_slot=False,
                            max_retries=self._config.retry_on_truncation,
                        )
                    else:
                        # Smart thinking param error handling
                        is_thinking, error_type, detail = _is_thinking_param_error(e)
                        if is_thinking:
                            model = body.get("model", "")
                            if error_type == "budget_exceeded":
                                # Reduce budget and retry WITH thinking
                                new_budget = int(detail) if detail and detail.isdigit() else 1024
                                if new_budget is not None:
                                    set_thinking_budget_override(model, new_budget)
                                logger.warning(
                                    "{}_STREAM: Budget exceeded for model ({}) - "
                                    "reducing reasoning_budget to {} and retrying WITH thinking",
                                    tag,
                                    model,
                                    new_budget,
                                )
                                if new_budget is not None:
                                    body = _rebuild_with_reduced_budget(body, new_budget)
                                stream = await self._global_rate_limiter.execute_with_retry(
                                    self._client.chat.completions.create,
                                    **body,
                                    stream=True,
                                    use_worker_slot=False,
                                    max_retries=self._config.retry_on_truncation,
                                )
                            elif error_type == "invalid_param":
                                # Remove just the invalid param and retry WITH thinking
                                if detail is not None:
                                    logger.warning(
                                        "{}_STREAM: Invalid param {} for model ({}) - "
                                        "removing param and retrying WITH thinking",
                                        tag,
                                        detail,
                                        model,
                                    )
                                    body = _rebuild_without_key(body, detail)
                                stream = await self._global_rate_limiter.execute_with_retry(
                                    self._client.chat.completions.create,
                                    **body,
                                    stream=True,
                                    use_worker_slot=False,
                                    max_retries=self._config.retry_on_truncation,
                                )
                            elif error_type == "effort_unsupported":
                                # Model rejects *_effort but accepts enable_thinking:
                                # drop only the effort flags and retry WITH thinking
                                logger.warning(
                                    "{}_STREAM: Effort flag {} rejected by model ({}) - "
                                    "retrying with enable_thinking only",
                                    tag,
                                    detail,
                                    model,
                                )
                                mark_effort_flags_unsupported(model)
                                body = _rebuild_without_effort_flags(body)
                                stream = await self._global_rate_limiter.execute_with_retry(
                                    self._client.chat.completions.create,
                                    **body,
                                    stream=True,
                                    use_worker_slot=False,
                                    max_retries=self._config.retry_on_truncation,
                                )
                            else:
                                # Generic "thinking not supported" → full fallback
                                logger.warning(
                                    "{}_STREAM: Thinking parameters rejected by model ({}) - "
                                    "retrying WITHOUT thinking parameters",
                                    tag,
                                    model,
                                )
                                mark_thinking_unsupported(model)
                                body = _rebuild_without_thinking(body)
                                stream = await self._global_rate_limiter.execute_with_retry(
                                    self._client.chat.completions.create,
                                    **body,
                                    stream=True,
                                    use_worker_slot=False,
                                    max_retries=self._config.retry_on_truncation,
                                )
                        else:
                            raise
                finally:
                    request_id_var.reset(req_token)

                try:
                    # Buffer thinking/reply content to avoid logging tiny fragments under high throughput
                    thinking_log_buffer = ""
                    reply_log_buffer = ""
                    LOG_FLUSH_SIZE = 120
                    chunks_received = 0

                    async for chunk in stream:
                        chunks_received += 1
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
                            # Flush log buffers on finish
                            if thinking_log_buffer:
                                self._live_nim_reply(thinking_log_buffer, kind="THINKING")
                                thinking_log_buffer = ""
                            if reply_log_buffer:
                                self._live_nim_reply(reply_log_buffer, kind="REPLY")
                                reply_log_buffer = ""

                        # Handle reasoning_content (OpenAI extended format)
                        reasoning = getattr(delta, "reasoning_content", None)
                        if reasoning:
                            thinking_log_buffer += reasoning
                            if len(thinking_log_buffer) >= LOG_FLUSH_SIZE:
                                self._live_nim_reply(thinking_log_buffer, kind="THINKING")
                                thinking_log_buffer = ""
                            for event in sse.ensure_thinking_block():
                                yield event
                            yield sse.emit_thinking_delta(reasoning)

                        # Handle text content
                        if delta.content:
                            reply_log_buffer += delta.content
                            if len(reply_log_buffer) >= LOG_FLUSH_SIZE:
                                self._live_nim_reply(reply_log_buffer, kind="REPLY")
                                reply_log_buffer = ""
                            for part in think_parser.feed(delta.content):
                                if part.type == ContentType.THINKING:
                                    for event in sse.ensure_thinking_block():
                                        yield event
                                    yield sse.emit_thinking_delta(part.content)
                                else:
                                    # First pass text through DSML parser to catch DSML tool calls
                                    dsml_text, dsml_tool_calls = dsml_parser.feed(part.content)
                                    # Then pass the remaining text to heuristic parser for regular tool calls
                                    filtered_text, detected_tools = heuristic_parser.feed(dsml_text)

                                    # Emit DSML tool calls first
                                    for tool_use in dsml_tool_calls:
                                        for event in sse.close_content_blocks():
                                            yield event
                                        block_idx = sse.blocks.allocate_index()
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

                    # Flush any remaining log buffers at stream end
                    if thinking_log_buffer:
                        self._live_nim_reply(thinking_log_buffer, kind="THINKING")
                        thinking_log_buffer = ""
                    if reply_log_buffer:
                        self._live_nim_reply(reply_log_buffer, kind="REPLY")
                        reply_log_buffer = ""


                    # Detect truncated/malformed response: stream completed without finish_reason.
                    # NVIDIA sometimes cuts off the connection mid-response — partial content with
                    # no finish_reason means the stream was truncated.
                    if finish_reason is None:
                        has_partial_content = bool(
                            sse.accumulated_text or sse.accumulated_reasoning or sse.blocks.tool_states
                        )
                        if chunks_received == 0:
                            logger.warning(
                                "{}_STREAM: NVIDIA returned an empty SSE body "
                                "(200 OK but 0 chunks, no finish_reason) - "
                                "treating as retryable error",
                                tag,
                            )
                            raise StreamTruncatedError(
                                "NVIDIA backend returned empty SSE body (0 chunks)"
                            )
                        elif not has_partial_content:
                            logger.warning(
                                "{}_STREAM: Stream completed with no content or finish_reason - "
                                "treating as retryable error",
                                tag,
                            )
                            raise StreamTruncatedError(
                                "NVIDIA backend stream completed with no content"
                            )
                        else:
                            # Partial content was delivered before the stream cut.
                            # If that partial content is reasoning-only (no assistant text
                            # and no tool calls), Claude Code cannot act on it (thinking is
                            # hidden) — delivering it just produces an empty turn that triggers
                            # a re-prompt loop. Treat it as truncation and retry for real output
                            # instead.
                            if sse.accumulated_reasoning and not sse.accumulated_text and not sse.blocks.tool_states:
                                logger.warning(
                                    "{}_STREAM: Stream truncated mid-response with reasoning-only "
                                    "content ({} chars reasoning, no text, no tool calls) - treating "
                                    "as retryable truncation instead of delivering",
                                    tag,
                                    len(sse.accumulated_reasoning),
                                )
                                raise StreamTruncatedError(
                                    f"NVIDIA backend stream truncated with reasoning-only content "
                                    f"({len(sse.accumulated_reasoning)} chars reasoning, no assistant text)"
                                )
                            # Otherwise mark the stream truncated and let
                            # _stream_response_impl's post-stream flush close the
                            # blocks and emit message_delta/message_stop (emitting
                            # them here as well produced duplicate message_stop,
                            # which Claude Code rejects as malformed). The post-
                            # stream flush also appends _PARTIAL_RESPONSE_NOTE so
                            # Claude knows the reply was cut short and can adapt.
                            sse.mark_truncated(
                                "Partial response: upstream stream cut mid-response"
                            )
                            logger.warning(
                                "{}_STREAM: Stream truncated mid-response - {} chars text, {} chars reasoning, "
                                "{} tool calls delivered but no finish_reason. Delivering partial response "
                                "instead of retrying.",
                                tag,
                                len(sse.accumulated_text),
                                len(sse.accumulated_reasoning),
                                len(sse.blocks.tool_states),
                            )

                    # Detect "thinking-only" truncation: finish_reason is set (e.g. "length"
                    # from max_tokens) but the stream produced only reasoning with no usable
                    # assistant text and no tool calls. DeepSeek can burn the entire token
                    # budget on thinking and return finish_reason="length" with zero reply.
                    # Claude Code cannot act on reasoning alone (thinking content is hidden),
                    # so we retry to get actual text/tool calls rather than delivering nothing useful.
                    if finish_reason and not sse.accumulated_text and not sse.blocks.tool_states:
                        logger.warning(
                            "{}_STREAM: finish_reason={} but stream produced only {} chars reasoning "
                            "and no assistant text or tool calls - treating as retryable truncation",
                            tag,
                            finish_reason,
                            len(sse.accumulated_reasoning),
                        )
                        raise StreamTruncatedError(
                            f"NVIDIA backend returned finish_reason={finish_reason} with "
                            "reasoning-only content (no assistant text)"
                        )
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
                InternalServerError,
                APIStatusError,
                httpx.HTTPStatusError,
                APIError,
                NotFoundError,
                StreamTruncatedError,
                # Raw httpx errors surface during body iteration (the OpenAI SDK
                # wraps them during the initial create() call but NOT in the
                # async-for chunk loop). These must be in the except tuple here
                # or the is_retryable check below never sees them.
                httpx.ReadError,
                httpx.TimeoutException,
                # "peer closed connection without sending complete message body
                # (incomplete chunked read)" - NVIDIA dropping mid-body. Not a
                # ReadError subclass (sibling under TransportError), so it must
                # be listed explicitly.
                httpx.RemoteProtocolError,
            ) as e:
                # Check if it's a retryable error
                # Note: APIConnectionError, APITimeoutError, httpx.ReadError,
                # httpx.TimeoutException, httpx.RemoteProtocolError during chunk
                # iteration are retried here (execute_with_retry only covers the
                # initial create() call, not the async-for loop where mid-stream
                # drops actually occur).
                # Safe to retry: enqueue_stream buffers all events, so the client
                # only receives the final attempt's clean SSE sequence.
                is_retryable = (
                    isinstance(e, (APIConnectionError, APITimeoutError, httpx.ReadError, httpx.TimeoutException, httpx.RemoteProtocolError))
                    or _is_retryable_server_error(e)
                    or _is_resource_exhausted_error(e)
                    or isinstance(e, (StreamTruncatedError, NotFoundError))
                    or ("service temporarily overloaded" in str(e).lower())
                )
                last_error_tag = f"{type(e).__name__}"
                detail = (
                    _format_error_detail(e)
                    + _timeout_subtype(
                        e,
                        read=self._config.http_read_timeout,
                        connect=self._config.http_connect_timeout,
                        write=self._config.http_write_timeout,
                    )
                    + (f" {_req_ctx(request_id, body)}" if _req_ctx(request_id, body) else "")
                )

                # ResourceExhausted errors have their own retry count
                if _is_resource_exhausted_error(e):
                    effective_max_retries = self._config.resource_exhausted_retries
                else:
                    effective_max_retries = max_retries

                if is_retryable:
                    if effective_max_retries == 0 or attempt < effective_max_retries:
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
                    # Non-retryable error - check if it might be caused by thinking params
                    is_thinking_related = _has_thinking_params(body) and (
                        "internal server error" in detail.lower()
                        or "internal error" in detail.lower()
                        or "server error" in detail.lower()
                    )
                    if is_thinking_related:
                        model = body.get("model", "")
                        logger.warning(
                            "{}_STREAM: Non-retryable error with thinking params enabled ({}) - "
                            "retrying without thinking parameters",
                            tag,
                            model,
                        )
                        mark_thinking_unsupported(model)
                        # Rebuild body in-place without thinking params
                        new_body = _rebuild_without_thinking(body)
                        body.clear()
                        body.update(new_body)
                        attempt += 1
                        raise  # Will be caught by outer retry loop

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