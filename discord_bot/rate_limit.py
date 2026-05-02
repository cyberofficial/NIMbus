"""Multi-layer rate limiting for Discord bot."""

import asyncio
import time
from collections import deque
from typing import Tuple


class DiscordRateLimiter:
    """
    Discord-specific rate limiting before hitting global NIM rate limit.

    Layers:
    1. Per-user cooldown (prevents spam)
    2. Per-guild server limit (protects NIM API quota)
    3. Per-channel lock (prevents channel flooding)
    """

    def __init__(
        self,
        user_cooldown: float = 10.0,  # Seconds between user requests
        server_limit: int = 20,       # Requests per window
        server_window: float = 60.0,   # Window in seconds
    ):
        self._user_cooldown = user_cooldown
        self._user_last_request: dict[int, float] = {}  # user_id -> timestamp

        self._server_limit = server_limit
        self._server_window = server_window
        self._server_requests: deque[float] = deque()  # timestamps
        self._server_lock = asyncio.Lock()

        self._channel_locks: dict[int, asyncio.Lock] = {}

    async def check_user_rate(self, user_id: int, channel_id: int = 0) -> Tuple[bool, float]:
        """Check if user can make request. Returns (allowed, retry_after)."""
        now = time.monotonic()
        key = (user_id, channel_id); last = self._user_last_request.get(key, 0)
        elapsed = now - last

        if elapsed < self._user_cooldown:
            return False, self._user_cooldown - elapsed

        self._user_last_request[key] = now
        return True, 0.0

    async def check_server_rate(self) -> Tuple[bool, float]:
        """Check server-wide rate limit. Returns (allowed, retry_after)."""
        async with self._server_lock:
            now = time.monotonic()
            cutoff = now - self._server_window

            # Remove old requests
            while self._server_requests and self._server_requests[0] <= cutoff:
                self._server_requests.popleft()

            if len(self._server_requests) >= self._server_limit:
                retry_after = self._server_requests[0] + self._server_window - now
                return False, retry_after

            self._server_requests.append(now)
            return True, 0.0

    def acquire_channel_lock(self, channel_id: int) -> asyncio.Lock:
        """Get or create lock for channel concurrency control."""
        if channel_id not in self._channel_locks:
            self._channel_locks[channel_id] = asyncio.Lock()
        return self._channel_locks[channel_id]

    async def cleanup_channel(self, channel_id: int) -> None:
        """Remove channel lock when channel is deleted."""
        if channel_id in self._channel_locks:
            del self._channel_locks[channel_id]
