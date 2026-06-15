"""Model swapper state manager - per-API-key in-memory storage."""

import asyncio
from typing import ClassVar


class ModelSwapManager:
    """Thread-safe in-memory storage for model swaps keyed by API key."""

    _swaps: ClassVar[dict[str, str]] = {}  # api_key -> swapped_model
    _lock: ClassVar[asyncio.Lock] = asyncio.Lock()

    @classmethod
    async def get(cls, api_key: str) -> str | None:
        """Get swapped model for API key, or None if no swap active."""
        async with cls._lock:
            return cls._swaps.get(api_key)

    @classmethod
    async def set(cls, api_key: str, model: str) -> None:
        """Set swapped model for API key."""
        async with cls._lock:
            cls._swaps[api_key] = model

    @classmethod
    async def clear(cls, api_key: str) -> None:
        """Clear swapped model for API key."""
        async with cls._lock:
            cls._swaps.pop(api_key, None)

    @classmethod
    async def has_swap(cls, api_key: str) -> bool:
        """Check if API key has an active swap."""
        async with cls._lock:
            return api_key in cls._swaps

    @classmethod
    async def get_all(cls) -> dict[str, str]:
        """Get all active swaps (for debugging/admin)."""
        async with cls._lock:
            return cls._swaps.copy()

    @classmethod
    async def clear_all(cls) -> None:
        """Clear all swaps (for testing)."""
        async with cls._lock:
            cls._swaps.clear()
