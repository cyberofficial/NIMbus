"""HTTPX transport that captures response headers for rate limit parsing."""

import threading
from typing import ClassVar

import httpx
from loguru import logger


class CapturedHeaders:
    """Thread-safe singleton for captured response headers."""

    _instance: ClassVar[CapturedHeaders | None] = None

    def __init__(self) -> None:
        self._storage: dict[int, dict[str, str]] = {}
        self._lock = threading.Lock()
        self._request_counter = 0

    @classmethod
    def get_instance(cls) -> CapturedHeaders:
        """Get singleton instance."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """Reset singleton (for testing)."""
        cls._instance = None

    def set_headers(self, request_id: int, headers: dict[str, str]) -> None:
        """Store headers for a request."""
        with self._lock:
            self._storage[request_id] = headers

    def get_headers(self, request_id: int) -> dict[str, str] | None:
        """Get and remove headers for a request."""
        with self._lock:
            return self._storage.pop(request_id, None)

    def clear_all(self) -> None:
        """Clear all stored headers."""
        with self._lock:
            self._storage.clear()


class HeaderCapturingTransport(httpx.AsyncHTTPTransport):
    """HTTPX transport that captures response headers for rate limit parsing.

    Captures headers from each response and stores them in the CapturedHeaders
    singleton for later retrieval and parsing.
    """

    def __init__(self, capture_store: CapturedHeaders, **kwargs) -> None:
        """Initialize transport with header capture.

        Args:
            capture_store: Singleton for storing captured headers
            **kwargs: Additional arguments passed to AsyncHTTPTransport
        """
        super().__init__(**kwargs)
        self._capture_store = capture_store

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        """Handle request and capture response headers.

        Args:
            request: The HTTP request to handle

        Returns:
            HTTP response with headers captured
        """
        # Debug: confirm transport is being used
        print(
            f"🚀 HeaderCapturingTransport handling request to {request.url}", flush=True
        )

        response = await super().handle_async_request(request)

        # Capture headers for rate limit parsing
        request_id = id(request)
        headers = dict(response.headers)

        # Debug: show all headers received
        print(f"📋 Response headers: {list(headers.keys())}", flush=True)

        # Filter to rate-limit-related headers only
        # Try multiple header patterns: x-ratelimit-*, retry-after, nvcf-*
        rate_limit_headers = {
            k: v
            for k, v in headers.items()
            if k.lower().startswith(("x-ratelimit", "retry-after", "x-request"))
            or k.lower() in ("nvcf-reqid", "nvcf-status")
        }

        if rate_limit_headers:
            self._capture_store.set_headers(request_id, rate_limit_headers)
            # Use print for visibility (like rate limit status bar)
            print(
                f"📥 Captured rate limit headers: {rate_limit_headers}",
                flush=True,
            )
            logger.debug(
                f"Captured rate limit headers for request {request_id}: "
                f"{rate_limit_headers}"
            )
            # Store request_id on response for retrieval
            response._rate_limit_request_id = request_id  # type: ignore[attr-defined]

        return response
