"""HTTPX transport that captures response headers for rate limit parsing."""

import time
import threading
from contextvars import ContextVar
from typing import ClassVar

import httpx
from loguru import logger

# Context variable to carry request_id from provider into the transport layer
request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)


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
        # Log outgoing request with correlation ID
        method = request.method
        corr_id = request_id_var.get()
        corr_tag = f" ({corr_id})" if corr_id else ""
        print(f"→ {method} {request.url}{corr_tag}", flush=True)
        start_time = time.monotonic()

        response = await super().handle_async_request(request)

        elapsed = time.monotonic() - start_time

        # Capture headers for rate limit parsing
        req_int_id = id(request)
        headers = dict(response.headers)

        # Filter to rate-limit-related headers only
        rate_limit_headers = {
            k: v
            for k, v in headers.items()
            if k.lower().startswith(("x-ratelimit", "retry-after", "x-request"))
            or k.lower() in ("nvcf-reqid", "nvcf-status")
        }

        if rate_limit_headers:
            self._capture_store.set_headers(req_int_id, rate_limit_headers)

            nvcf_id = rate_limit_headers.get("nvcf-reqid", "?")
            nvcf_status = rate_limit_headers.get("nvcf-status", "fulfilled")

            # Build error detail string when request failed
            status_code = response.status_code
            if status_code >= 400 or nvcf_status == "errored":
                reason = response.reason_phrase or ""
                error_detail = f" {status_code} {reason}".strip() if reason else f" HTTP {status_code}"
            else:
                error_detail = ""

            print(
                f"← nvcf-reqid: {nvcf_id}  ✓ {nvcf_status}{error_detail}{corr_tag}"
                f" Took {elapsed:.1f}s",
                flush=True,
            )
            logger.debug(
                f"Captured rate limit headers for request {req_int_id}: "
                f"{rate_limit_headers}"
            )
            # Store request_id on response for retrieval
            response._rate_limit_request_id = req_int_id  # type: ignore[attr-defined]
        else:
            # No rate-limit headers but still log timing for every request
            status_code = response.status_code
            if status_code >= 400:
                reason = response.reason_phrase or ""
                error_detail = f" {status_code} {reason}".strip() if reason else f" HTTP {status_code}"
                print(f"← {method} {response.url}{error_detail}{corr_tag} Took {elapsed:.1f}s", flush=True)

        return response
