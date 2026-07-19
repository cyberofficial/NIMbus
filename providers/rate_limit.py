"""Global rate limiter for API requests.

Uses server-provided headers for proactive rate limiting and retry-after for reactive blocking.
Auto-restores to initial limits after N successful requests without 429 (NIM_RPM_RESET messages).
Also provides worker-aware concurrency control to respect NVIDIA NIM's per-worker request limit.
"""

import asyncio
import time
from collections import deque
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any, ClassVar, TypeVar

import httpx
import openai
from loguru import logger
from openai import APIConnectionError, APITimeoutError

T = TypeVar("T")


class GlobalRateLimiter:
    """
    Global singleton rate limiter that:
    - Proactively throttles using sliding window (limit from x-ratelimit-limit header or config fallback)
    - Reactively blocks on 429 using retry-after header
    - Auto-restores to initial limits after N successful requests without 429 (NIM_RPM_RESET messages)
    - Enforces worker concurrency limit (default 32) to respect NVIDIA NIM worker limits
    - expose <nimrpm:reset> to clear reactive block
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
        # Auto-restore after N successful requests without 429 (0 = disabled)
        rpm_reset: int = 5,
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
        if rpm_reset < 0:
            raise ValueError("rpm_reset must be >= 0")

        # ---- Static config ----
        self._rate_window = float(rate_window)
        self._initial_rpm = rate_limit
        self._current_rpm = rate_limit  # Updated from x-ratelimit-limit header
        self._rpm_reset = rpm_reset  # Number of successful requests to auto-restore

        # ---- Sliding-window state ----
        self._request_times: deque[float] = deque()
        self._blocked_until: float = 0
        self._lock = asyncio.Lock()
        self._concurrency_sem = asyncio.Semaphore(max_concurrency)

        # ---- Worker-aware concurrency ----
        self._worker_limit = worker_limit
        self._worker_sem = asyncio.Semaphore(worker_limit)

        # ---- Auto-restore state ----
        self._success_count = 0

        # ---- Adaptive cap state ----
        self._capped_rpm: int | None = None  # Track capped RPM level (None = not capped)

        self._initialized = True

        logger.info(
            f"GlobalRateLimiter initialized "
            f"(initial={rate_limit} rpm, window={rate_window}s, "
            f"auto_restore_after={rpm_reset} successful requests, "
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
        rpm_reset: int | None = None,
    ) -> GlobalRateLimiter:
        """Get or create the singleton instance.

        Args:
            rate_limit: Requests per window (only used on first creation)
            rate_window: Window in seconds (only used on first creation)
            max_concurrency: Max simultaneous open streams
            worker_limit: Max concurrent requests per NVIDIA NIM worker
            rpm_reset: Auto-restore to initial RPM after N successful requests without 429 (0=disabled)
        """
        if cls._instance is None:
            cls._instance = cls(
                rate_limit=rate_limit or 40,
                rate_window=rate_window or 60.0,
                max_concurrency=max_concurrency,
                worker_limit=worker_limit,
                rpm_reset=rpm_reset or 5,
            )
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """Reset singleton (for testing)."""
        cls._instance = None

    # ------------------------------------------------------------------
    # Public methods
    # ------------------------------------------------------------------

    @property
    def current_rpm(self) -> int:
        """Current effective RPM (from server headers or fallback)."""
        return self._current_rpm

    @property
    def worker_limit(self) -> int:
        return self._worker_limit

    @asynccontextmanager
    async def worker_slot(self) -> AsyncIterator[None]:
        """Acquire a worker slot for NVIDIA NIM worker concurrency control."""
        await self._worker_sem.acquire()
        self._log_worker_status()
        try:
            yield
        finally:
            self._worker_sem.release()

    def _log_worker_status(self) -> None:
        """Log NVIDIA worker slot usage to console."""
        worker_limit = self._worker_limit
        worker_used = worker_limit - (self._worker_sem._value if hasattr(self._worker_sem, '_value') else 0)
        worker_pct = (worker_used / worker_limit) * 100 if worker_limit > 0 else 0
        bar_width = 30
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

    async def acquire_worker_slot(self) -> None:
        """Acquire a worker slot (use with try/finally release)."""
        await self._worker_sem.acquire()

    def release_worker_slot(self) -> None:
        """Release a worker slot."""
        self._worker_sem.release()

    def reset_reactive_block(self) -> None:
        """Clear reactive block and adaptive cap (e.g. via <nimrpm:reset>)."""
        self._blocked_until = 0
        if self._capped_rpm is not None:
            old = self._current_rpm
            self._current_rpm = self._initial_rpm
            self._capped_rpm = None
            logger.info(f"↺ Manual reset: {old} → {self._initial_rpm} rpm via <nimrpm:reset>")
        else:
            logger.info("↺ Reactive rate limit block cleared via <nimrpm:reset>")

    def on_success(self) -> None:
        """Call after a successful request (no 429).

        Increments success counter. When capped, gradually recovers RPM by +1
        per rpm_reset successful requests until reaching initial_rpm.
        """
        if self._rpm_reset <= 0:
            return
        self._success_count += 1
        if self._success_count >= self._rpm_reset:
            self._success_count = 0
            # Gradual recovery when capped
            if self._capped_rpm is not None:
                if self._current_rpm < self._initial_rpm:
                    old = self._current_rpm
                    self._current_rpm = min(self._initial_rpm, self._current_rpm + 1)
                    self._capped_rpm = self._current_rpm
                    logger.info(f"↺ Gradual recovery: {old} → {self._current_rpm} rpm after {self._rpm_reset} successful requests")
                else:
                    # Reached initial_rpm, clear cap
                    logger.info(f"↺ Full recovery: {self._current_rpm} → {self._initial_rpm} rpm")
                    self._capped_rpm = None

    def on_rate_limited(self) -> None:
        """Call when a 429 is received. Resets success counter and applies adaptive cap.

        Caps to the current effective rate (sliding window count) rather than the
        configured/server limit, since the 429 indicates the true limit is lower.
        """
        self._success_count = 0

        # Calculate effective RPM from sliding window (requests in current window)
        now = time.monotonic()
        cutoff = now - self._rate_window
        # Count requests still in window
        effective_count = sum(1 for t in self._request_times if t > cutoff)
        # Use at least 1, at most current_rpm
        effective_rpm = max(1, min(effective_count, self._current_rpm))

        if self._capped_rpm is None:
            # First 429 at this level - cap to effective RPM
            self._capped_rpm = effective_rpm
            self._current_rpm = effective_rpm
            logger.warning(f"⚠️ Rate limited - capping RPM to effective rate: {self._capped_rpm}")
        elif self._current_rpm == self._capped_rpm:
            # 429 at capped RPM - decrement by 1 (floor at 1)
            if self._capped_rpm > 1:
                old = self._capped_rpm
                self._capped_rpm -= 1
                self._current_rpm = self._capped_rpm
                logger.warning(f"⚠️ Rate limited at cap - dropping RPM {old} → {self._capped_rpm}")
            else:
                logger.warning(f"⚠️ Rate limited at minimum RPM (1)")
        # else: 429 but _current_rpm != _capped_rpm (shouldn't happen with proper sync)

    # ------------------------------------------------------------------
    # Wait logic (used by provider)
    # ------------------------------------------------------------------

    async def wait_if_blocked(self) -> bool:
        """
        Wait if currently rate limited or throttle to meet quota.

        Returns:
            True if was reactively blocked and waited, False otherwise.
        """
        now = time.monotonic()

        # 1. Reactive check: Wait if blocked by retry-after
        waited_reactively = False
        if now < self._blocked_until:
            wait_time = self._blocked_until - now
            logger.warning(
                f"Global provider rate limit active (reactive), waiting {wait_time:.1f}s..."
            )
            await asyncio.sleep(wait_time)
            waited_reactively = True

        # 2. Proactive check: strict rolling window using current_rpm
        await self._acquire_proactive_slot()
        return waited_reactively

    async def _acquire_proactive_slot(self) -> None:
        """Acquire a proactive slot enforcing a strict rolling window."""
        while True:
            wait_time = 0.0
            async with self._lock:
                now = time.monotonic()
                cutoff = now - self._rate_window

                while self._request_times and self._request_times[0] <= cutoff:
                    self._request_times.popleft()

                if len(self._request_times) < self._current_rpm:
                    self._request_times.append(now)
                    self._log_rate_limit_status()
                    return

                oldest = self._request_times[0]
                wait_time = max(0.0, (oldest + self._rate_window) - now)

            if wait_time > 0:
                await asyncio.sleep(wait_time)
            else:
                await asyncio.sleep(0)

    async def release_last_slot(self) -> None:
        """Release the most recently acquired rate limit slot.

        This is used when a request fails before reaching the server (e.g., timeout,
        connection error) to remove the slot from the sliding window without waiting.
        """
        async with self._lock:
            if self._request_times:
                self._request_times.pop()
                logger.debug("Rate limit slot released due to failed request before response")

    def _log_rate_limit_status(self) -> None:
        """Log current rate limit usage to console."""
        current = len(self._request_times)
        remaining = self._current_rpm - current
        percentage = (current / self._current_rpm) * 100 if self._current_rpm > 0 else 100

        # Calculate time until oldest request expires
        reset_in = 0.0
        if self._request_times:
            now = time.monotonic()
            oldest = self._request_times[0]
            reset_in = max(0.0, (oldest + self._rate_window) - now)

        # Create visual bar
        bar_width = 30
        filled = int((current / self._current_rpm) * bar_width) if self._current_rpm > 0 else bar_width
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
                f"{emoji} [Rate Limit] [{bar}] {current}/{self._current_rpm} ({percentage:.0f}%) | "
                f"{remaining} left | Next Slot free in {reset_in:.1f}s"
            )
        else:
            logger.info(
                f"{emoji} [Rate Limit] [{bar}] {current}/{self._current_rpm} ({percentage:.0f}%) | "
                f"{remaining} left"
            )

    def set_blocked(self, seconds: float = 60) -> None:
        """Set reactive block for specified seconds (from retry-after header)."""
        self._blocked_until = time.monotonic() + seconds
        logger.warning(f"Global provider rate limit set for {seconds:.1f}s (reactive from retry-after)")

    def is_blocked(self) -> bool:
        """Check if currently reactively blocked."""
        return time.monotonic() < self._blocked_until

    def remaining_wait(self) -> float:
        """Get remaining reactive wait time in seconds."""
        return max(0.0, self._blocked_until - time.monotonic())

    def get_status(self) -> dict[str, Any]:
        """Get current rate limit status."""
        now = time.monotonic()
        cutoff = now - self._rate_window

        # Clean old requests
        while self._request_times and self._request_times[0] <= cutoff:
            self._request_times.popleft()

        current = len(self._request_times)
        remaining = self._current_rpm - current

        # Calculate time until oldest request expires
        reset_in = 0.0
        if self._request_times:
            oldest = self._request_times[0]
            reset_in = max(0.0, (oldest + self._rate_window) - now)

        # Worker slot availability
        worker_available = self._worker_sem._value if hasattr(self._worker_sem, '_value') else self._worker_limit

        return {
            "current": current,
            "max": self._current_rpm,
            "initial_max": self._initial_rpm,
            "remaining": remaining,
            "reset_in_seconds": reset_in,
            "is_blocked": self.is_blocked(),
            "blocked_seconds_remaining": self.remaining_wait(),
            "worker_limit": self._worker_limit,
            "worker_available": worker_available,
            "capped_rpm": self._capped_rpm,
            "initial_rpm": self._initial_rpm,
        }

    def parse_rate_limit_headers(self, headers: dict[str, str]) -> float | None:
        """Parse rate limit response headers and return retry-after seconds.

        Checks:
        - ``x-ratelimit-limit`` (updates current RPM)
        - ``x-ratelimit-remaining`` (logged for visibility)
        - ``x-ratelimit-reset`` (updates rate window)
        - ``retry-after`` (returned as float seconds if present)

        Args:
            headers: Dictionary of HTTP response headers

        Returns:
            Retry-After value in seconds, or None if not present.
        """
        retry_after: float | None = None

        # Parse x-ratelimit-limit → update current RPM (only if not capped)
        limit = headers.get("x-ratelimit-limit")
        if limit and self._capped_rpm is None:
            try:
                new_limit = int(limit)
                if new_limit > 0 and new_limit != self._current_rpm:
                    logger.info(f"📤 Rate limit updated from header: {self._current_rpm} → {new_limit} rpm")
                    self._current_rpm = new_limit
            except (ValueError, TypeError):
                logger.debug(f"Could not parse x-ratelimit-limit: {limit}")

        # Parse x-ratelimit-reset → update rate window
        reset = headers.get("x-ratelimit-reset")
        if reset:
            try:
                reset_time = float(reset)
                now = time.time()
                if reset_time > now:  # Absolute timestamp
                    new_window = reset_time - now
                    if new_window > 0 and new_window != self._rate_window:
                        logger.info(f"📤 Rate window updated from header: {self._rate_window:.1f} → {new_window:.1f}s")
                        self._rate_window = new_window
                else:  # Relative seconds
                    if reset_time > 0 and reset_time != self._rate_window:
                        logger.info(f"📤 Rate window updated from header: {self._rate_window:.1f} → {reset_time:.1f}s")
                        self._rate_window = reset_time
            except (ValueError, TypeError):
                logger.debug(f"Could not parse x-ratelimit-reset: {reset}")

        # Parse remaining for visibility
        remaining = headers.get("x-ratelimit-remaining")
        if remaining:
            logger.debug(f"Rate limit remaining from headers: {remaining}")

        # Parse Retry-After header
        raw = headers.get("retry-after") or headers.get("Retry-After")
        if raw:
            try:
                retry_after = float(raw)
                logger.info("📤 Parsed Retry-After header: {:.0f}s", retry_after)
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
        """Execute an async callable with rate limiting and retry on retryable errors.

        Waits for the proactive limiter before each attempt. On 429, applies
        reactive block from retry-after header before retrying. On other retryable
        errors (timeout, connection error), releases the rate limit slot before retrying
        since those requests never reached the server.

        Args:
            fn: Async callable to execute.
            max_retries: Maximum number of retry attempts after the first failure.
                         Use 0 for infinite retries (retry forever).
            base_delay: Base delay in seconds for exponential backoff (fallback if no retry-after).
            max_delay: Maximum delay cap in seconds.
            jitter: Maximum random jitter in seconds added to each delay.
            use_worker_slot: Whether to acquire a worker slot (default True).

        Returns:
            The result of the callable.

        Raises:
            The last exception if all retries are exhausted.
        """
        import random
        last_exc: Exception | None = None
        attempt = 0

        # HTTP status codes that indicate retryable server errors
        retryable_error_codes = {500, 502, 503, 504}

        def is_retryable_error(e: Exception) -> bool:
            """Check if error is retryable (timeout/connection) or rate limit."""
            if isinstance(e, openai.RateLimitError):
                return True
            if isinstance(e, (
                httpx.ConnectTimeout,
                httpx.ReadTimeout,
                httpx.WriteTimeout,
                httpx.ConnectError,
                httpx.ReadError,
                httpx.TimeoutException,
                APITimeoutError,
                APIConnectionError,
            )):
                return True
            # Check for 5xx status codes
            response = getattr(e, "response", None)
            if response is not None:
                status_code = getattr(response, "status_code", None)
                if status_code in retryable_error_codes:
                    return True
            return False

        # max_retries=0 means infinite retries
        while max_retries == 0 or attempt <= max_retries:
            await self.wait_if_blocked()

            # Acquire worker slot for the actual API call
            async with self.worker_slot() if use_worker_slot else self._noop_context():
                try:
                    result = await fn(*args, **kwargs)
                    self.on_success()  # Track success for auto-restore
                    return result
                except openai.RateLimitError as e:
                    last_exc = e
                    self.on_rate_limited()

                    # Parse retry-after header
                    retry_seconds = base_delay * (2**attempt)  # fallback
                    if hasattr(e, "response") and e.response is not None:
                        headers = dict(e.response.headers)
                        retry_after = headers.get("retry-after") or headers.get("Retry-After")
                        if retry_after:
                            try:
                                retry_seconds = float(retry_after)
                            except (ValueError, TypeError):
                                pass

                    if max_retries > 0 and attempt >= max_retries:
                        logger.warning(
                            f"Rate limit retry exhausted after {max_retries} retries"
                        )
                        break

                    total_attempts = max_retries + 1 if max_retries > 0 else "∞"
                    logger.warning(
                        f"Rate limited (429), attempt {attempt + 1}/{total_attempts}. "
                        f"Retrying in {retry_seconds:.1f}s..."
                    )
                    self.set_blocked(retry_seconds)
                    await asyncio.sleep(retry_seconds)
                    attempt += 1
                    continue

                except Exception as e:
                    # Non-429 retryable errors (timeout, connection) - release the slot
                    # since the request never reached the server
                    if is_retryable_error(e):
                        last_exc = e
                        await self.release_last_slot()
                        if max_retries > 0 and attempt >= max_retries:
                            logger.warning(
                                f"Retryable error exhausted after {max_retries} retries: {type(e).__name__}"
                            )
                            break
                        logger.warning(
                            f"Retryable error ({type(e).__name__}) on attempt {attempt + 1}/{max_retries if max_retries > 0 else '∞'} - retrying"
                        )
                        # Exponential backoff with jitter
                        retry_seconds = min(base_delay * (2**attempt), max_delay)
                        retry_seconds += random.uniform(0, jitter)
                        await asyncio.sleep(retry_seconds)
                        attempt += 1
                        continue
                    raise

        assert last_exc is not None
        raise last_exc

    @asynccontextmanager
    async def _noop_context(self) -> AsyncIterator[None]:
        """No-op context manager for when worker slot is disabled."""
        yield