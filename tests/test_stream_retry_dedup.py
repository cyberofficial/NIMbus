"""Tests for stream_response duplicate message_start dedup on retries.

When a streaming request is retried (connection drop / truncation / overload),
`_stream_response_impl` emits a fresh `message_start` for every attempt, so the
buffered event list passed to `stream_response` can contain multiple message
sequences. Claude Code only accepts a single message per response, so
`stream_response` must keep only the events from the final attempt's
`message_start` onward and drop the partial earlier ones.

These tests also guard the logging line added with the fix (a placeholder/arg
count mismatch there would raise IndexError and crash the whole response).
"""

import httpx
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from config.nim import NimSettings
from providers.base import ProviderConfig
from providers.exceptions import StreamTruncatedError
from providers.provider import NvidiaNimProvider


@pytest.fixture
def config():
    """Minimal ProviderConfig for testing."""
    return ProviderConfig(
        api_key="test-key",
        base_url="https://integrate.api.nvidia.com/v1",
        rate_limit=40,
        rate_window=60,
        max_concurrency=5,
        http_read_timeout=300.0,
        http_write_timeout=10.0,
        http_connect_timeout=2.0,
        server_type="stream",
        max_wait_time=30.0,
        retry_on_truncation=3,
        retry_delay=0.01,
    )


@pytest.fixture
def nim_settings():
    """Minimal NimSettings."""
    return NimSettings(
        max_tokens=32000,
        thinking=True,
        reasoning_effort="high",
    )


@pytest.fixture
def provider(config, nim_settings):
    """Create a NvidiaNimProvider with mocked queue / client."""
    with patch("providers.provider.HeaderCapturingTransport", autospec=True), \
         patch("providers.provider.httpx.AsyncClient", autospec=True), \
         patch("providers.provider.AsyncOpenAI"), \
         patch("providers.provider.GlobalRateLimiter") as mock_limiter_class:

        mock_limiter = mock_limiter_class.get_instance.return_value
        mock_limiter.worker_slot = MagicMock()
        mock_limiter.worker_slot.return_value.__aenter__ = AsyncMock()
        mock_limiter.worker_slot.return_value.__aexit__ = AsyncMock(
            return_value=False
        )

        prov = NvidiaNimProvider(config, nim_settings=nim_settings)
        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock()
        prov._client = mock_client

        # Provide a pageable queue mock so stream_response path is reachable.
        q = MagicMock()
        q.is_enabled = True
        prov._request_queue = q

        yield prov


@pytest.fixture
def mock_request():
    """Create a minimal mock request object resembling MessagesRequest."""
    req = MagicMock()
    req.model = "nvidia/nemotron-3-super-120b-a12b"
    req.max_tokens = 32000
    req.temperature = None
    req.top_p = None
    req.top_k = -1
    req.extra_body = None
    req.thinking = None
    return req


def _attempt_events(prefix_id: str) -> list[str]:
    """Build a minimal valid-single-attempt SSE sequence."""
    return [
        f"event: message_start\ndata: {{\"type\":\"message_start\",\"id\":\"{prefix_id}\"}}",
        "event: content_block_start\ndata: {\"type\":\"content_block_start\"}",
        "event: content_block_delta\ndata: {\"type\":\"content_block_delta\",\"text\":\"hello\"}",
        "event: content_block_stop\ndata: {\"type\":\"content_block_stop\"}",
        "event: message_delta\ndata: {\"type\":\"message_delta\",\"stop_reason\":\"end_turn\"}",
        "event: message_stop\ndata: {\"type\":\"message_stop\"}",
    ]


@pytest.mark.asyncio
async def test_stream_response_single_attempt_unchanged(provider, mock_request):
    """No retry -> buffer has exactly one message_start -> events unchanged."""
    single = _attempt_events("final")
    provider._request_queue.enqueue_stream = AsyncMock(return_value=single)

    collected = [ev async for ev in provider.stream_response(mock_request)]

    assert collected == single
    assert sum("message_start" in ev for ev in collected) == 1
    assert any("message_stop" in ev for ev in collected)


@pytest.mark.asyncio
async def test_stream_response_drops_earlier_attempts_on_retry(provider, mock_request):
    """Retry leaves multiple message_start; keep only the final attempt's events."""
    first_attempt = _attempt_events("attempt-1")[:-2]  # truncation: no message_stop/delta
    final_attempt = _attempt_events("attempt-2")

    # enqueue_stream returns the concatenation of both attempts (how the real
    # queue buffers _stream_response_impl's multi-attempt output).
    buffered = first_attempt + final_attempt
    provider._request_queue.enqueue_stream = AsyncMock(return_value=buffered)

    collected = [ev async for ev in provider.stream_response(mock_request)]

    # Exactly one message_start survives - the final attempt's.
    starts = [ev for ev in collected if "message_start" in ev]
    assert len(starts) == 1
    assert "attempt-2" in starts[0]

    # The full final sequence (including message_stop) is preserved.
    assert any("message_stop" in ev for ev in collected)
    assert collected == final_attempt


@pytest.mark.asyncio
async def test_stream_response_no_index_error_on_retry(provider, mock_request):
    """Regression: the dedup logging must not raise IndexError (placeholder/arg
    mismatch would crash stream_response -> ASGI -> 'empty or malformed')."""
    first_attempt = _attempt_events("attempt-1")[:-2]
    final_attempt = _attempt_events("attempt-2")
    provider._request_queue.enqueue_stream = AsyncMock(
        return_value=first_attempt + final_attempt
    )

    # The entire async iteration must complete without raising.
    collected = [ev async for ev in provider.stream_response(mock_request)]

    starts = [ev for ev in collected if "message_start" in ev]
    assert len(starts) == 1
    assert starts[0].startswith("event: message_start")


# ---------------------------------------------------------------------------
# Tests: mid-stream retry of raw httpx errors + empty-body truncation
# ---------------------------------------------------------------------------


def _content_delta_event() -> str:
    """A minimal content_block_delta SSE event (used as a healthy stream body)."""
    return (
        "event: content_block_delta\n"
        "data: {\"type\":\"content_block_delta\",\"delta\":{\"type\":\"text_delta\",\"text\":\"ok\"}}"
    )


async def _collect_impl(provider, mock_request) -> list[str]:
    """Drive `_stream_response_impl` directly to exercise its retry loop."""
    return [
        ev
        async for ev in provider._stream_response_impl(
            mock_request,
            input_tokens=10,
            request_id="req_test",
            use_rate_limiter_concurrency=False,
        )
    ]


@pytest.mark.asyncio
async def test_stream_retries_on_midstream_read_timeout(provider, mock_request):
    """A raw httpx.ReadTimeout during chunk iteration must be retried.

    Regression: the OpenAI SDK surfaces read timeouts as raw httpx exceptions
    (not APITimeoutError) inside `async for chunk in stream`. They must be in
    the except tuple or they fall through to the non-retryable catch-all.
    """
    calls = {"n": 0}

    async def _fake_do_stream(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ReadTimeout("idle read timeout mid-stream")
        yield _content_delta_event()

    with patch.object(provider, "_build_request_body", return_value={"model": "test-model"}), \
         patch("providers.provider.get_settings") as mock_gs, \
         patch.object(provider, "_do_stream_request", _fake_do_stream):
        mock_gs.return_value.nim.thinking = False
        collected = await _collect_impl(provider, mock_request)

    assert calls["n"] == 2, "read timeout should have been retried once"
    # One message_start per attempt (dedup happens in stream_response, not here).
    assert len([ev for ev in collected if "message_start" in ev]) == 2
    assert any("message_stop" in ev for ev in collected)


@pytest.mark.asyncio
async def test_stream_retries_on_empty_body_truncation(provider, mock_request):
    """A zero-chunk 'fulfilled' stream (StreamTruncatedError) must be retried."""
    calls = {"n": 0}

    async def _fake_do_stream(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise StreamTruncatedError("NVIDIA returned an empty SSE body (0 chunks)")
        yield _content_delta_event()

    with patch.object(provider, "_build_request_body", return_value={"model": "test-model"}), \
         patch("providers.provider.get_settings") as mock_gs, \
         patch.object(provider, "_do_stream_request", _fake_do_stream):
        mock_gs.return_value.nim.thinking = False
        collected = await _collect_impl(provider, mock_request)

    assert calls["n"] == 2, "empty-body truncation should have been retried once"
    assert any("message_stop" in ev for ev in collected)


@pytest.mark.asyncio
async def test_stream_retries_are_bounded_by_max_retries(provider, mock_request):
    """The outer retry loop must increment attempt and eventually stop.

    Regression: `attempt` was never incremented in the outer loop, so a
    persistently-failing stream retried forever with zero backoff.
    """
    calls = {"n": 0}

    async def _always_fail(*args, **kwargs):
        calls["n"] += 1
        raise StreamTruncatedError("persistent truncation")
        yield  # noqa: unreachable, but required to make this an async generator

    with patch.object(provider, "_build_request_body", return_value={"model": "test-model"}), \
         patch("providers.provider.get_settings") as mock_gs, \
         patch.object(provider, "_do_stream_request", _always_fail):
        mock_gs.return_value.nim.thinking = False
        collected = await _collect_impl(provider, mock_request)

    # config.retry_on_truncation = 3 -> attempts 0,1,2,3 == 4 total calls.
    assert calls["n"] == provider._config.retry_on_truncation + 1
    # Graceful degraded stream still terminates with a clean message_stop.
    assert any("message_stop" in ev for ev in collected)


@pytest.mark.asyncio
async def test_stream_retries_on_thinking_only_truncation(provider, mock_request):
    """A stream with finish_reason='length' but only reasoning (no text, no tools)
    must be treated as retryable truncation.

    Regression: DeepSeek can burn the entire token budget on thinking and return
    finish_reason='length' with zero assistant text. Claude Code cannot act on
    reasoning alone — the stream must be retried so the next attempt can deliver
    an actual response.
    """
    calls = {"n": 0}

    def _make_thinking_only_stream():
        """Simulate a stream that yields only reasoning and ends with finish_reason='length'."""
        import types

        async def _gen():
            # Yield a chunk with reasoning content
            chunk1 = MagicMock()
            chunk1.usage = None
            chunk1.choices = [
                MagicMock(
                    delta=MagicMock(content=None, reasoning_content="Thinking deeply..."),
                    finish_reason=None,
                )
            ]
            yield chunk1

            # Yield final chunk with finish_reason="length" but no text
            chunk2 = MagicMock()
            chunk2.usage = MagicMock(prompt_tokens=100, completion_tokens=5000)
            chunk2.choices = [
                MagicMock(
                    delta=MagicMock(content=None, reasoning_content=None),
                    finish_reason="length",
                )
            ]
            yield chunk2

        return _gen()

    with patch.object(provider, "_build_request_body", return_value={"model": "deepseek-test"}), \
         patch("providers.provider.get_settings") as mock_gs, \
         patch.object(provider._client.chat.completions, "create", new_callable=AsyncMock) as mock_create, \
         patch.object(provider._global_rate_limiter, "execute_with_retry", side_effect=lambda fn, **kw: fn(**kw)):
        mock_gs.return_value.nim.thinking = True
        # First call returns thinking-only stream, second call returns healthy stream
        healthy_stream = _make_thinking_only_stream()
        # For the second attempt, we need a stream with actual text
        async def _healthy_gen():
            chunk = MagicMock()
            chunk.usage = MagicMock(prompt_tokens=100, completion_tokens=50)
            chunk.choices = [
                MagicMock(
                    delta=MagicMock(content="Hello!", reasoning_content=None),
                    finish_reason="stop",
                )
            ]
            yield chunk

        mock_create.side_effect = [_make_thinking_only_stream(), _healthy_gen()]
        collected = await _collect_impl(provider, mock_request)

    assert calls["n"] == 0  # execute_with_retry is mocked, so we count via mock_create
    assert mock_create.call_count == 2, "thinking-only truncation should have been retried once"
    assert any("message_stop" in ev for ev in collected)
