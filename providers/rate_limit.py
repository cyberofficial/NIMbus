"""Global rate limiter for API requests with adaptive backoff.

When a 429 is encountered, this limiter progressively reduces the effective
RPM rate limit and, once a floor is reached, starts introducing per-request
hold delays.  The adaptive state can be reset via <nimrpm:reset>.

Also provides worker-aware concurrency control to respect NVIDIA NIM's
per-worker request limit (default 32 concurrent requests per worker).
"""

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

    **Adaptive backoff** – on every 429 the limiter will:
      1. Drop the effective RPM by ``rpm_drop`` (default 10) until
         ``rpm_min`` is reached.
      2. Once at the floor, introduce a per-request hold delay that
         progresses from 0 → ``hold_initial`` → ``hold_max``.

    Call ``reset_adaptive_backoff()`` to restore initial values
    (e.g. via ``<nimrpm:reset>``).

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
        # Worker-aware concurrency (NVIDIA NIM worker limit)
        worker_limit: int = 32,
        # Adaptive backoff parameters
        rpm_drop: int = 10,
        rpm_min: int = 20,
        hold_initial: float = 5.0,
        hold_max: float = 10.0,
    ):
        if hasattr(self, "_initialized"):
            return

        if rate_limit <= 0:
            raise ValueError("rate_limit must be > 0")
        if rate_window <= 0:
            raise ValueError("rate_window must be > 0")
        if max_concurrency <= 0:
            raise ValueError("max_concurrency must be > 0")
        if worker_limit <= 0:
            raise ValueError("worker_limit must be > 0")

        # ---- Static config ----
        self._rate_window = float(rate_window)
        self._initial_rpm = rate_limit
        self._rpm_drop = rpm_drop
        self._rpm_min = rpm_min
        self._hold_initial = hold_initial
        self._hold_max = hold_max

        # ---- Adaptive state ----
        self._effective_rpm = rate_limit
        self._drop_count = 0
        self._hold_delay = 0.0  # seconds to wait before each request

        # ---- Sliding-window state ----
        self._request_times: deque[float] = deque()
        self._blocked_until: float = 0
        self._lock = asyncio.Lock()
        self._concurrency_sem = asyncio.Semaphore(max_concurrency)

        # ---- Worker-aware concurrency ----
        self._worker_limit = worker_limit
        self._worker_sem = asyncio.Semaphore(worker_limit)

        self._initialized = True

        logger.info(
            f"GlobalRateLimiter initialized "
            f"(initial={rate_limit} rpm, window={rate_window}s, "
            f"drop={rpm_drop}, min={rpm_min}, "
            f"hold_initial={hold_initial}s, hold_max={hold_max}s, "
            f"max_concurrency={max_concurrency}, worker_limit={worker_limit})"
        )

    # ------------------------------------------------------------------
    # Singleton
    # ------------------------------------------------------------------

    @classmethod
    def get_instance(
        cls,
        rate_limit: int | None = None,
        rate_window: float | None = None,
        max_concurrency: int = 5,
        worker_limit: int = 32,
        rpm_drop: int | None = None,
        rpm_min: int | None = None,
        hold_initial: float | None = None,
        hold_max: float | None = None,
    ) -> GlobalRateLimiter:
        """Get or create the singleton instance.

        Args:
            rate_limit: Requests per window (only used on first creation)
            rate_window: Window in seconds (only used on first creation)
            max_concurrency: Max simultaneous open streams
            worker_limit: Max concurrent requests per NVIDIA NIM worker
            rpm_drop: RPM reduction per 429 hit
            rpm_min: Floor RPM before hold delays activate
            hold_initial: First hold delay in seconds
            hold_max: Maximum hold delay in seconds
        """
        if cls._instance is None:
            cls._instance = cls(
                rate_limit=rate_limit or 40,
                rate_window=rate_window or 60.0,
                max_concurrency=max_concurrency,
                worker_limit=worker_limit,
                rpm_drop=rpm_drop or 10,
                rpm_min=rpm_min or 20,
                hold_initial=hold_initial or 5.0,
                hold_max=hold_max or 10.0,
            )
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """Reset singleton (for testing)."""
        cls._instance = None

    # ------------------------------------------------------------------
    # Adaptive backoff
    # ------------------------------------------------------------------

    def on_rate_limit_hit(self) -> None:
        """Called when a 429 is received.

        Drops the effective RPM or increases the hold delay.
        """
        if self._effective_rpm > self._rpm_min:
            old = self._effective_rpm
            self._effective_rpm = max(self._rpm_min, old - self._rpm_drop)
            self._drop_count += 1
            logger.warning(
                "⚠️ Adaptive rate limit: dropped RPM {} → {} "
                "(drop #{}, min={})",
                old,
                self._effective_rpm,
                self._drop_count,
                self._rpm_min,
            )
        else:
            # At floor – increase hold delay
            if self._hold_delay < self._hold_initial:
                self._hold_delay = self._hold_initial
            elif self._hold_delay < self._hold_max:
                self._hold_delay = min(self._hold_max, self._hold_delay + self._hold_initial)
            logger.warning(
                "⚠️ Adaptive rate limit: at floor RPM ({}), "
                "increased hold delay to {:.1f}s (max={:.1f}s)",
                self._effective_rpm,
                self._hold_delay,
                self._hold_max,
            )

    @property
    def effective_rpm(self) -> int:
        return self._effective_rpm

    @property
    def hold_delay(self) -> float:
        return self._hold_delay

    # ---- Worker-aware concurrency ----

    @property
    def worker_limit(self) -> int:
        return self._worker_limit

    @asynccontextmanager
    async def worker_slot(self) -> AsyncIterator[None]:
        """Acquire a worker slot for NVIDIA NIM worker concurrency control.

        This limits concurrent requests to the NVIDIA worker limit (default 32)
        to avoid "Worker local total request limit reached" errors.
        """
        await self._worker_sem.acquire()
        try:
            yield
        finally:
            self._worker_sem.release()

    async def acquire_worker_slot(self) -> None:
        """Acquire a worker slot (use with try/finally release)."""
        await self._worker_sem.acquire()

    def release_worker_slot(self) -> None:
        """Release a worker slot."""
        self._worker_sem.release()

    def reset_adaptive_backoff(self) -> None:
        """Restore initial RPM and clear hold delay."""
        old_rpm = self._effective_rpm
        old_hold = self._hold_delay
        self._effective_rpm = self._initial_rpm
        self._drop_count = 0
        self._hold_delay = 0.0
        logger.info(
            "↺ Adaptive backoff reset: RPM {} → {}, hold {:.1f}s → 0",
            old_rpm,
            self._effective_rpm,
            old_hold,
        )

    # ------------------------------------------------------------------
    # Wait logic (used by provider)
    # ------------------------------------------------------------------

    async def wait_if_blocked(self) -> bool:
        """
        Wait if currently rate limited or throttle to meet quota.

        Also applies adaptive hold delay when one is active.

        Returns:
            True if was reactively blocked and waited, False otherwise.
        """
        # 0. Adaptive hold delay – small pause before each request
        if self._hold_delay > 0:
            logger.info(
                "⏳ Adaptive hold delay active: waiting {:.1f}s before request",
                self._hold_delay,
            )
            await asyncio.sleep(self._hold_delay)

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

        # 2. Proactive check: strict rolling window (uses effective_rpm)
        await self._acquire_proactive_slot()
        return waited_reactively

    async def _acquire_proactive_slot(self) -> None:
        """
        Acquire a proactive slot enforcing a strict rolling window
        using the *effective* (adaptively reduced) RPM.
        """
        while True:
            wait_time = 0.0
            async with self._lock:
                now = time.monotonic()
                cutoff = now - self._rate_window

                while self._request_times and self._request_times[0] <= cutoff:
                    self._request_times.popleft()

                if len(self._request_times) < self._effective_rpm:
                    self._request_times.append(now)
                    self._log_rate_limit_status()
                    return

                oldest = self._request_times[0]
                wait_time = max(0.0, (oldest + self._rate_window) - now)

            if wait_time > 0:
                await asyncio.sleep(wait_time)
            else:
                await asyncio.sleep(0)

    def _log_rate_limit_status(self) -> None:
        """Log current rate limit usage to console."""
        current = len(self._request_times)
        remaining = self._effective_rpm - current
        percentage = (current / self._effective_rpm) * 100 if self._effective_rpm > 0 else 100

        # Calculate time until oldest request expires
        reset_in = 0.0
        if self._request_times:
            now = time.monotonic()
            oldest = self._request_times[0]
            reset_in = max(0.0, (oldest + self._rate_window) - now)

        # Create visual bar
        bar_width = 30
        filled = int((current / self._effective_rpm) * bar_width) if self._effective_rpm > 0 else bar_width
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
                f"{emoji} [Rate Limit] [{bar}] {current}/{self._effective_rpm} ({percentage:.0f}%) | "
                f"{remaining} left | Resets in {reset_in:.1f}s"
            )
        else:
            logger.info(
                f"{emoji} [Rate Limit] [{bar}] {current}/{self._effective_rpm} ({percentage:.0f}%) | "
                f"{remaining} left"
            )

        # Worker status bar (NVIDIA concurrent request slots)
        worker_limit = self._worker_limit
        worker_used = worker_limit - (self._worker_sem._value if hasattr(self._worker_sem, '_value') else 0)
        worker_pct = (worker_used / worker_limit) * 100 if worker_limit > 0 else 0
        w_filled = int((worker_used / worker_limit) * bar_width) if worker_limit > 0 else bar_width
        w_empty = bar_width - w_filled
        w_bar = "█" * w_filled + "░" * w_empty
        if worker_pct >= 90:
            w_emoji = "🔴"
        elif worker_pct >= 70:
            w_emoji = "🟡"
        else:
            w_emoji = "🟢"
        logger.info(
            f"{w_emoji} [Workers   ] [{w_bar}] {worker_used}/{worker_limit} ({worker_pct:.0f}%) | "
            f"{worker_limit - worker_used} free"
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
            Dict with keys: current, max, remaining, reset_in_seconds,
            is_blocked, effective_rpm, hold_delay, drop_count,
            worker_limit, worker_available
        """
        now = time.monotonic()
        cutoff = now - self._rate_window

        # Clean old requests
        while self._request_times and self._request_times[0] <= cutoff:
            self._request_times.popleft()

        current = len(self._request_times)
        remaining = self._effective_rpm - current

        # Calculate time until oldest request expires
        reset_in = 0.0
        if self._request_times:
            oldest = self._request_times[0]
            reset_in = max(0.0, (oldest + self._rate_window) - now)

        # Worker slot availability
        worker_available = self._worker_sem._value if hasattr(self._worker_sem, '_value') else self._worker_limit

        return {
            "current": current,
            "max": self._effective_rpm,
            "initial_max": self._initial_rpm,
            "remaining": remaining,
            "reset_in_seconds": reset_in,
            "is_blocked": self.is_blocked(),
            "blocked_seconds_remaining": self.remaining_wait(),
            "effective_rpm": self._effective_rpm,
            "hold_delay": self._hold_delay,
            "drop_count": self._drop_count,
            "worker_limit": self._worker_limit,
            "worker_available": worker_available,
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
            self._effective_rpm = rate_limit
            self._initial_rpm = rate_limit
            logger.info(f"Rate limit updated to {rate_limit} requests per window")
        if rate_window is not None and rate_window > 0:
            self._rate_window = float(rate_window)
            logger.info(f"Rate window updated to {rate_window}s")

    def parse_rate_limit_headers(self, headers: dict[str, str]) -> float | None:
        """Parse rate limit response headers and return retry-after seconds.

        Checks:
        - ``x-ratelimit-limit`` (updates adaptive RPM if present)
        - ``x-ratelimit-remaining`` (logged for visibility)
        - ``x-ratelimit-reset`` (logged for visibility)
        - ``retry-after`` (returned as float seconds if present)

        Args:
            headers: Dictionary of HTTP response headers

        Returns:
            Retry-After value in seconds, or None if not present.
        """
        retry_after: float | None = None

        # Parse x-ratelimit-limit
        limit = headers.get("x-ratelimit-limit")
        if limit:
            try:
                self.update_limits(rate_limit=int(limit))
            except (ValueError, TypeError):
                logger.debug(f"Could not parse x-ratelimit-limit: {limit}")

        # Parse remaining for visibility
        remaining = headers.get("x-ratelimit-remaining")
        if remaining:
            logger.debug(f"Rate limit remaining from headers: {remaining}")

        # Parse reset time for visibility
        reset = headers.get("x-ratelimit-reset")
        if reset:
            logger.debug(f"Rate limit resets at: {reset}")

        # Parse Retry-After header
        raw = headers.get("retry-after") or headers.get("Retry-After")
        if raw:
            try:
                retry_after = float(raw)
                logger.info(
                    "📤 Parsed Retry-After header: {:.0f}s", retry_after
                )
            except (ValueError, TypeError):
                logger.debug(f"Could not parse Retry-After: {raw}")

        return retry_after

    # ------------------------------------------------------------------
    # Concurrency & retry
    # ------------------------------------------------------------------

    @asynccontextmanager
    async def concurrency_slot(self) -> AsyncIterator[None]:
        """Async context manager that holds one concurrency slot for a stream."""
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
        use_worker_slot: bool = True,
        **kwargs: Any,
    ) -> Any:
        """Execute an async callable with rate limiting and retry on 429.

        Waits for the proactive limiter before each attempt. On 429, applies
        exponential backoff with jitter before retrying.
        Also calls ``on_rate_limit_hit()`` to trigger adaptive backoff.

        Args:
            fn: Async callable to execute.
            max_retries: Maximum number of retry attempts after the first failure.
            base_delay: Base delay in seconds for exponential backoff.
            max_delay: Maximum delay cap in seconds.
            jitter: Maximum random jitter in seconds added to each delay.
            use_worker_slot: Whether to acquire a worker slot (default True).

        Returns:
            The result of the callable.

        Raises:
            The last exception if all retries are exhausted.
        """
        last_exc: Exception | None = None

        for attempt in range(1 + max_retries):
            await self.wait_if_blocked()

            # Acquire worker slot for the actual API call
            async with self.worker_slot() if use_worker_slot else self._noop_context():
                try:
                    return await fn(*args, **kwargs)
                except openai.RateLimitError as e:
                    last_exc = e
                    # Fire adaptive backoff
                    self.on_rate_limit_hit()

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

    @asynccontextmanager
    async def _noop_context(self) -> AsyncIterator[None]:
        """No-op context manager for when worker slot is disabled."""
        yield