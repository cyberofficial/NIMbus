"""Dependency injection for FastAPI.

This module provides NVIDIA NIM provider exclusively.
"""

from fastapi import HTTPException
from loguru import logger

from config.settings import Settings
from config.settings import get_settings as _get_settings
from providers import NVIDIA_NIM_BASE_URL, NvidiaNimProvider
from providers.base import BaseProvider, ProviderConfig
from providers.error_mapping import get_user_facing_error_message
from providers.exceptions import AuthenticationError
from providers.rate_limit import GlobalRateLimiter

# Global provider instance (singleton)
_provider: BaseProvider | None = None


def get_settings() -> Settings:
    """Get application settings via dependency injection."""
    return _get_settings()


def _create_provider(settings: Settings) -> BaseProvider:
    """Construct and return a new NVIDIA NIM provider instance from settings.

    This codebase exclusively uses NVIDIA NIM API endpoints.
    """
    if not settings.api_key or not settings.api_key.strip():
        raise AuthenticationError(
            "NVIDIA_NIM_API_KEY is not set. Add it to your .env file. "
            "Get a key at https://build.nvidia.com/settings/api-keys"
        )

    # Initialize global rate limiter with correct config BEFORE any request
    # can trigger error_mapping (which would create it with defaults).
    GlobalRateLimiter.get_instance(
        rate_limit=settings.provider_rate_limit,
        rate_window=settings.provider_rate_window,
        max_concurrency=settings.provider_max_concurrency,
        worker_limit=settings.request_queue_max_concurrent,
        rpm_reset=settings.nim_rpm_reset,
    )

    config = ProviderConfig(
        api_key=settings.api_key,
        base_url=NVIDIA_NIM_BASE_URL,
        rate_limit=settings.provider_rate_limit,
        rate_window=settings.provider_rate_window,
        max_concurrency=settings.provider_max_concurrency,
        http_read_timeout=settings.http_read_timeout,
        http_write_timeout=settings.http_write_timeout,
        http_connect_timeout=settings.http_connect_timeout,
        server_type=settings.server_type,
        show_nvidia_reply=settings.show_nvidia_reply,
        max_wait_time=settings.provider_max_wait_time,
        retry_on_truncation=settings.provider_retry_on_truncation,
        retry_delay=settings.provider_retry_delay,
        resource_exhausted_retries=settings.provider_resource_exhausted_retries,
        # Request queue settings
        request_queue_enabled=settings.request_queue_enabled,
        request_queue_max_concurrent=settings.request_queue_max_concurrent,
        request_queue_max_size=settings.request_queue_max_size,
        request_queue_timeout=settings.request_queue_timeout,
        request_queue_num_workers=settings.request_queue_num_workers,
        # Adaptive rate limiting
        rpm_reset=settings.nim_rpm_reset,
        # Hidden auto-compact
        hidden_compact=settings.hidden_compact,
    )
    provider = NvidiaNimProvider(config, nim_settings=settings.nim)
    logger.info("Provider initialized: NVIDIA NIM")
    return provider


def get_provider() -> BaseProvider:
    """Get or create the NVIDIA NIM provider instance."""
    global _provider
    if _provider is None:
        try:
            _provider = _create_provider(get_settings())
        except AuthenticationError as e:
            raise HTTPException(
                status_code=503, detail=get_user_facing_error_message(e)
            ) from e
    return _provider


async def cleanup_provider():
    """Cleanup provider resources."""
    global _provider
    if _provider:
        await _provider.cleanup()
        _provider = None
    logger.debug("Provider cleanup completed")
