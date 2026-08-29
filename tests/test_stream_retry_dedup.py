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

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from config.nim import NimSettings
from providers.base import ProviderConfig
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
