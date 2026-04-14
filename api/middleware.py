"""API middleware for authentication and security."""

from fastapi import Request
from fastapi.responses import JSONResponse
from loguru import logger


async def verify_api_key(request: Request, call_next):
    """Verify API key for protected routes.

    Checks for API key in:
    - Header: X-API-Key
    - Header: Authorization (Bearer token)
    - Query param: api_key

    Public routes (no auth required):
    - /health
    - /
    """
    from config.settings import get_settings

    from .bot_protection import check_bot_protection

    settings = get_settings()
    api_key = settings.proxy_api_key

    # Extract API key first to check if request is authenticated
    provided_key = (
        request.headers.get("X-API-Key")
        or request.headers.get("Authorization", "").replace("Bearer ", "")
        or request.query_params.get("api_key")
    )

    # Check bot protection first (but skip if valid API key is provided)
    # Valid API key users bypass bot protection
    if not api_key or provided_key != api_key:
        bot_response = await check_bot_protection(request)
        if bot_response:
            return bot_response

    # Skip auth if no proxy_api_key is set (backward compatibility)
    if not api_key:
        return await call_next(request)

    # Public routes that don't require auth
    public_paths = [
        "/health",
        "/status",
        "/",
        "/favicon.ico",
        "/docs",
        "/openapi.json",
        "/redoc",
    ]
    if request.url.path in public_paths or request.url.path.startswith("/docs"):
        return await call_next(request)

    # Verify API key for protected routes
    if provided_key != api_key:
        logger.warning(
            f"Unauthorized access attempt from {request.client.host if request.client else 'unknown'} to {request.url.path}"
        )
        return JSONResponse(
            status_code=401,
            content={
                "type": "error",
                "error": {
                    "type": "authentication_error",
                    "message": "Invalid API key. Please check your PROXY_API_KEY in .env file.",
                },
            },
        )

    return await call_next(request)
