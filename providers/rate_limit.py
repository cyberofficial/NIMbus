"""Global rate limiter for API requests."""

import asyncio
import random
import time
from collections import deque
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any, ClassVar, TypeVar

import openai
from loguru import logger

T = TypeVar("T")


class GlobalRateLimiter:
    """
    Global singleton rate limiter that blocks all requests
    when a rate limit error is encountered (reactive) and
    throttles requests (proactive) using a strict rolling window.

    Optionally enforces a max_concurrency cap: at most N provider streams
    may be open simultaneously, independent of the sliding window.

    Proactive limits - throttles requests to stay within API limits.
    Reactive limits - pauses all requests when a 429 is hit.
    Concurrency limit - caps simultaneously open streams.
    """

    _instance: ClassVar[GlobalRateLimiter | None] = None

    def __new__(cls, *args: Any, **kwargs: Any) -> GlobalRateLimiter:
        if cls._instance is not None:
            return cls._instance
        instance = super().__new__(cls)
        return instance

    def __init__(
        self,
        rate_limit: int = 40,
        rate_window: float = 60.0,
        max_concurrency: int = 5,
    ):
        # Prevent re-initialization on singleton reuse
        if hasattr(self, "_initialized"):
            return

        if rate_limit <= 0:
            raise ValueError("rate_limit must be > 0")
        if rate_window <= 0:
            raise ValueError("rate_window must be > 0")
        if max_concurrency <= 0:
            raise ValueError("max_concurrency must be > 0")

        self._rate_limit = rate_limit
        self._rate_window = float(rate_window)
        # Monotonic timestamps of the last granted slots.
        self._request_times: deque[float] = deque()
        self._blocked_until: float = 0
        self._lock = asyncio.Lock()
        self._concurrency_sem = asyncio.Semaphore(max_concurrency)
        self._initialized = True

        logger.info(
            f"GlobalRateLimiter (Provider) initialized ({rate_limit} req / {rate_window}s, max_concurrency={max_concurrency})"
        )

    @classmethod
    def get_instance(
        cls,
        rate_limit: int | None = None,
        rate_window: float | None = None,
        max_concurrency: int = 5,
    ) -> GlobalRateLimiter:
        """Get or create the singleton instance.

        Args:
            rate_limit: Requests per window (only used on first creation)
            rate_window: Window in seconds (only used on first creation)
            max_concurrency: Max simultaneous open streams (only used on first creation)
        """
        if cls._instance is None:
            cls._instance = cls(
                rate_limit=rate_limit or 40,
                rate_window=rate_window or 60.0,
                max_concurrency=max_concurrency,
            )
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """Reset singleton (for testing)."""
        cls._instance = None

    async def wait_if_blocked(self) -> bool:
        """
        Wait if currently rate limited or throttle to meet quota.

        Returns:
            True if was reactively blocked and waited, False otherwise.
        """
        # 1. Reactive check: Wait if someone hit a 429
        waited_reactively = False
        now = time.monotonic()
        if now < self._blocked_until:
            wait_time = self._blocked_until - now
            logger.warning(
                f"Global provider rate limit active (reactive), waiting {wait_time:.1f}s..."
            )
            await asyncio.sleep(wait_time)
            waited_reactively = True

        # 2. Proactive check: strict rolling window (no bursts beyond N in last W seconds)
        await self._acquire_proactive_slot()
        return waited_reactively

    async def _acquire_proactive_slot(self) -> None:
        """
        Acquire a proactive slot enforcing a strict rolling window.

        Guarantees: at most `self._rate_limit` acquisitions in any interval of length
        `self._rate_window` (seconds).
        """
        while True:
            wait_time = 0.0
            async with self._lock:
                now = time.monotonic()
                cutoff = now - self._rate_window

                while self._request_times and self._request_times[0] <= cutoff:
                    self._request_times.popleft()

                if len(self._request_times) < self._rate_limit:
                    self._request_times.append(now)
                    # Log current rate limit status
                    self._log_rate_limit_status()
                    return

                oldest = self._request_times[0]
                wait_time = max(0.0, (oldest + self._rate_window) - now)

            # Sleep outside the lock so other tasks can continue to queue.
            if wait_time > 0:
                await asyncio.sleep(wait_time)
            else:
                await asyncio.sleep(0)

    def _log_rate_limit_status(self) -> None:
        """Log current rate limit usage to console."""
        current = len(self._request_times)
        remaining = self._rate_limit - current
        percentage = (current / self._rate_limit) * 100

        # Calculate time until oldest request expires
        reset_in = 0.0
        if self._request_times:
            now = time.monotonic()
            oldest = self._request_times[0]
            reset_in = max(0.0, (oldest + self._rate_window) - now)

        # Create visual bar
        bar_width = 30
        filled = int((current / self._rate_limit) * bar_width)
        empty = bar_width - filled

        # Color code based on usage
        if percentage >= 90:
            emoji = "🔴"
        elif percentage >= 70:
            emoji = "🟡"
        else:
            emoji = "🟢"

        bar = "█" * filled + "░" * empty

        if reset_in > 0:
            logger.info(
                f"{emoji} [Rate Limit] [{bar}] {current}/{self._rate_limit} ({percentage:.0f}%) | "
                f"{remaining} left | Resets in {reset_in:.1f}s"
            )
        else:
            logger.info(
                f"{emoji} [Rate Limit] [{bar}] {current}/{self._rate_limit} ({percentage:.0f}%) | "
                f"{remaining} left"
            )

    def set_blocked(self, seconds: float = 60) -> None:
        """
        Set global block for specified seconds (reactive).

        Args:
            seconds: How long to block (default 60s)
        """
        self._blocked_until = time.monotonic() + seconds
        logger.warning(f"Global provider rate limit set for {seconds:.1f}s (reactive)")

    def is_blocked(self) -> bool:
        """Check if currently reactively blocked."""
        return time.monotonic() < self._blocked_until

    def remaining_wait(self) -> float:
        """Get remaining reactive wait time in seconds."""
        return max(0.0, self._blocked_until - time.monotonic())

    def get_status(self) -> dict[str, Any]:
        """Get current rate limit status.

        Returns:
            Dict with keys: current, max, remaining, reset_in_seconds, is_blocked
        """
        now = time.monotonic()
        cutoff = now - self._rate_window

        # Clean old requests
        while self._request_times and self._request_times[0] <= cutoff:
            self._request_times.popleft()

        current = len(self._request_times)
        remaining = self._rate_limit - current

        # Calculate time until oldest request expires
        reset_in = 0.0
        if self._request_times:
            oldest = self._request_times[0]
            reset_in = max(0.0, (oldest + self._rate_window) - now)

        return {
            "current": current,
            "max": self._rate_limit,
            "remaining": remaining,
            "reset_in_seconds": reset_in,
            "is_blocked": self.is_blocked(),
            "blocked_seconds_remaining": self.remaining_wait(),
        }

    def update_limits(
        self, rate_limit: int | None = None, rate_window: float | None = None
    ) -> None:
        """Update rate limits based on server feedback.

        Args:
            rate_limit: New maximum requests per window (ignored if None or <= 0)
            rate_window: New window duration in seconds (ignored if None or <= 0)
        """
        if rate_limit is not None and rate_limit > 0:
            self._rate_limit = rate_limit
            logger.info(f"Rate limit updated to {rate_limit} requests per window")
        if rate_window is not None and rate_window > 0:
            self._rate_window = float(rate_window)
            logger.info(f"Rate window updated to {rate_window}s")

    def parse_rate_limit_headers(self, headers: dict[str, str]) -> None:
        """Parse x-ratelimit-* headers and update state.

        Parses the following headers:
        - x-ratelimit-limit: Maximum requests per window
        - x-ratelimit-remaining: Remaining requests (logged for visibility)
        - x-ratelimit-reset: Reset timestamp (logged for visibility)
        - retry-after: Seconds to wait before retrying

        Args:
            headers: Dictionary of HTTP response headers
        """
        # Parse x-ratelimit-limit
        limit = headers.get("x-ratelimit-limit")
        if limit:
            try:
                self.update_limits(rate_limit=int(limit))
            except ValueError, TypeError:
                logger.debug(f"Could not parse x-ratelimit-limit: {limit}")

        # Parse remaining for visibility
        remaining = headers.get("x-ratelimit-remaining")
        if remaining:
            logger.debug(f"Rate limit remaining from headers: {remaining}")

        # Parse reset time for visibility
        reset = headers.get("x-ratelimit-reset")
        if reset:
            logger.debug(f"Rate limit resets at: {reset}")

    @asynccontextmanager
    async def concurrency_slot(self) -> AsyncIterator[None]:
        """Async context manager that holds one concurrency slot for a stream.

        Blocks until a slot is available (controlled by max_concurrency).
        """
        await self._concurrency_sem.acquire()
        try:
            yield
        finally:
            self._concurrency_sem.release()

    async def execute_with_retry(
        self,
        fn: Callable[..., Any],
        *args: Any,
        max_retries: int = 3,
        base_delay: float = 2.0,
        max_delay: float = 60.0,
        jitter: float = 1.0,
        **kwargs: Any,
    ) -> Any:
        """Execute an async callable with rate limiting and retry on 429.

        Waits for the proactive limiter before each attempt. On 429, applies
        exponential backoff with jitter before retrying.

        Args:
            fn: Async callable to execute.
            max_retries: Maximum number of retry attempts after the first failure.
            base_delay: Base delay in seconds for exponential backoff.
            max_delay: Maximum delay cap in seconds.
            jitter: Maximum random jitter in seconds added to each delay.

        Returns:
            The result of the callable.

        Raises:
            The last exception if all retries are exhausted.
        """
        last_exc: Exception | None = None

        for attempt in range(1 + max_retries):
            await self.wait_if_blocked()

            try:
                return await fn(*args, **kwargs)
            except openai.RateLimitError as e:
                last_exc = e
                if attempt >= max_retries:
                    logger.warning(
                        f"Rate limit retry exhausted after {max_retries} retries"
                    )
                    break

                delay = min(base_delay * (2**attempt), max_delay)
                delay += random.uniform(0, jitter)
                logger.warning(
                    f"Rate limited (429), attempt {attempt + 1}/{max_retries + 1}. "
                    f"Retrying in {delay:.1f}s..."
                )
                self.set_blocked(delay)
                await asyncio.sleep(delay)

        assert last_exc is not None
        raise last_exc
