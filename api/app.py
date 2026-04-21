"""FastAPI application factory and configuration."""

import asyncio
import contextlib
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from loguru import logger

from config.logging_config import configure_logging
from config.settings import get_settings
from providers.exceptions import ProviderError

from .dependencies import cleanup_provider
from .middleware import verify_api_key
from .routes import router

# Opt-in to future behavior for python-telegram-bot (kept for compatibility)
os.environ["PTB_TIMEDELTA"] = "1"

# Configure logging first (before any module logs)
_settings = get_settings()
configure_logging(_settings.log_file)


_SHUTDOWN_TIMEOUT_S = 5.0
_STATUS_UPDATE_INTERVAL = 10.0  # seconds


async def _rate_limit_status_updater():
    """Background task that prints rate limit status every 10 seconds."""
    from providers.rate_limit import GlobalRateLimiter

    while True:
        await asyncio.sleep(_STATUS_UPDATE_INTERVAL)

        limiter = GlobalRateLimiter.get_instance()
        status = limiter.get_status()
        current = status["current"]

        # Only show status if there are active requests
        if current == 0:
            continue

        max_req = status["max"]
        remaining = status["remaining"]
        reset_in = status["reset_in_seconds"]

        # Calculate percentage and choose emoji
        percentage = (current / max_req) * 100
        if percentage >= 90:
            emoji = "🔴"
        elif percentage >= 70:
            emoji = "🟡"
        else:
            emoji = "🟢"

        # Create visual bar
        bar_width = 20
        filled = int((current / max_req) * bar_width)
        bar = "█" * filled + "░" * (bar_width - filled)

        # Only show reset time if there are active requests
        if reset_in > 0:
            print(
                f"{emoji} Rate Limit: [{bar}] {current}/{max_req} ({percentage:.0f}%) | "
                f"{remaining} left | Resets in {reset_in:.1f}s",
                flush=True,
            )
        else:
            print(
                f"{emoji} Rate Limit: [{bar}] {current}/{max_req} ({percentage:.0f}%) | "
                f"{remaining} left",
                flush=True,
            )


async def _best_effort(
    name: str, awaitable, timeout_s: float = _SHUTDOWN_TIMEOUT_S
) -> None:
    """Run a shutdown step with timeout; never raise to callers."""
    try:
        await asyncio.wait_for(awaitable, timeout=timeout_s)
    except TimeoutError:
        logger.warning(f"Shutdown step timed out: {name} ({timeout_s}s)")
    except Exception as e:
        logger.warning(f"Shutdown step failed: {name}: {type(e).__name__}: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager."""
    logger.info("Starting Claude Code Proxy...")

    # Store in app state for access in routes
    app.state.messaging_platform = None
    app.state.message_handler = None
    app.state.cli_manager = None
    app.state.discord_bot = None

    settings = get_settings()

    # Discord Bot initialization (optional)
    discord_bot = None
    if settings.discord_enabled:
        try:
            from discord_bot.bot import NimbusDiscordBot
            from api.dependencies import get_provider

            provider = get_provider()
            discord_bot = NimbusDiscordBot(settings, provider)

            # Start bot in background task
            asyncio.create_task(discord_bot.start_bot())
            app.state.discord_bot = discord_bot
            logger.info("Discord bot started")
        except Exception as e:
            logger.error(f"Failed to start Discord bot: {e}")
    else:
        logger.info("Discord bot not configured (DISCORD_BOT_TOKEN or DISCORD_GUILD_ID missing)")

    # Start background rate limit status updater
    status_task = asyncio.create_task(_rate_limit_status_updater())

    yield

    # Cleanup
    if status_task:
        status_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await status_task

    # Discord bot cleanup
    if discord_bot:
        logger.info("Shutting down Discord bot...")
        await _best_effort("discord_bot", discord_bot.close_bot())

    # Provider cleanup
    logger.info("Shutdown requested, cleaning up...")
    await _best_effort("cleanup_provider", cleanup_provider())

    logger.info("Server shut down cleanly")


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(
        title="Claude Code Proxy",
        version="2.0.0",
        lifespan=lifespan,
    )

    # Add API key middleware
    app.middleware("http")(verify_api_key)

    # Register routes
    app.include_router(router)

    # Exception handlers
    @app.exception_handler(ProviderError)
    async def provider_error_handler(request: Request, exc: ProviderError):
        """Handle provider-specific errors and return Anthropic format."""
        logger.error(f"Provider Error: {exc.error_type} - {exc.message}")
        return JSONResponse(
            status_code=exc.status_code,
            content=exc.to_anthropic_format(),
        )

    @app.exception_handler(Exception)
    async def general_error_handler(request: Request, exc: Exception):
        """Handle general errors and return Anthropic format."""
        logger.error(f"General Error: {exc!s}")
        import traceback

        logger.error(traceback.format_exc())
        return JSONResponse(
            status_code=500,
            content={
                "type": "error",
                "error": {
                    "type": "api_error",
                    "message": "An unexpected error occurred.",
                },
            },
        )

    return app


# Default app instance for uvicorn
app = create_app()
