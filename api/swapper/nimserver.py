"""NIM server type swapper state manager - per-API-key in-memory storage.

Allows mid-session swapping between stream and buffer mode
without restarting the server or changing environment variables.
"""

import asyncio
from typing import ClassVar


class NimServerManager:
    """Thread-safe in-memory storage for server type overrides keyed by API key."""

    _overrides: ClassVar[dict[str, str]] = {}  # api_key -> server_type
    _lock: ClassVar[asyncio.Lock] = asyncio.Lock()

    @classmethod
    async def get(cls, api_key: str) -> str | None:
        """Get overridden server type for API key, or None if no override active."""
        async with cls._lock:
            return cls._overrides.get(api_key)

    @classmethod
    async def set(cls, api_key: str, server_type: str) -> None:
        """Set server type override for API key."""
        async with cls._lock:
            cls._overrides[api_key] = server_type

    @classmethod
    async def clear(cls, api_key: str) -> None:
        """Clear server type override for API key."""
        async with cls._lock:
            cls._overrides.pop(api_key, None)

    @classmethod
    async def has_override(cls, api_key: str) -> bool:
        """Check if API key has an active server type override."""
        async with cls._lock:
            return api_key in cls._overrides

    @classmethod
    async def get_all(cls) -> dict[str, str]:
        """Get all active overrides (for debugging/admin)."""
        async with cls._lock:
            return cls._overrides.copy()

    @classmethod
    async def clear_all(cls) -> None:
        """Clear all overrides (for testing)."""
        async with cls._lock:
            cls._overrides.clear()