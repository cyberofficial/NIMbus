"""Model swapper validator - validates and tests models against NVIDIA NIM."""

import asyncio
import random
import httpx
from loguru import logger

from config.settings import Settings, _to_full_nim_model

NVIDIA_NIM_BASE_URL = "https://integrate.api.nvidia.com/v1"
NVIDIA_MODELS_URL = f"{NVIDIA_NIM_BASE_URL}/models"

# Retry configuration for validation
VALIDATION_MAX_RETRIES = 3
VALIDATION_BASE_DELAY = 1.0  # seconds
VALIDATION_MAX_DELAY = 10.0  # seconds

# HTTP status codes that should trigger a retry
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


async def _execute_with_retry(coro_factory, max_retries=VALIDATION_MAX_RETRIES, base_delay=VALIDATION_BASE_DELAY, max_delay=VALIDATION_MAX_DELAY):
    """
    Execute a coroutine with retry on transient failures.

    Retries on: 429 (rate limit), 5xx server errors, network errors
    """
    attempt = 0
    last_error = None

    while max_retries == 0 or attempt < (max_retries + 1):
        try:
            if attempt > 0:
                delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
                delay += random.uniform(0, 0.5)
                logger.warning("Validation retry attempt {}/{} after {:.1f}s delay", attempt, max_retries if max_retries > 0 else "∞", delay)
                await asyncio.sleep(delay)

            return await coro_factory()

        except httpx.HTTPStatusError as e:
            last_error = e
            if e.response.status_code in _RETRYABLE_STATUS:
                logger.warning("Validation retryable HTTP error: {} {}", e.response.status_code, e)
                attempt += 1
                continue
            # Non-retryable HTTP error
            raise
        except (httpx.RequestError, httpx.TimeoutException, asyncio.TimeoutError) as e:
            last_error = e
            logger.warning("Validation retryable network/timeout error: {}", e)
            attempt += 1
            continue
        except Exception as e:
            # Any other exception - log and don't retry (could be programming error)
            logger.error("Validation non-retryable error: {} {}", type(e).__name__, e)
            raise

    logger.error("Validation exhausted all retries: {}", last_error)
    assert last_error is not None
    raise last_error


def _find_similar_models(full_model: str, max_results: int = 5) -> list[str]:
    """Find models from the same family in the NVIDIA catalog.

    Matches by org prefix and shared model name parts.
    E.g. 'deepseek-ai/deepseek-v4-flash' finds 'deepseek-ai/deepseek-v4-pro', etc.

    Args:
        full_model: Full NIM model ID (e.g. 'deepseek-ai/deepseek-v4-flash')
        max_results: Maximum number of suggestions to return

    Returns:
        List of similar model IDs (excluding the failed model itself)
    """
    from config.settings import _fetch_nvidia_models

    try:
        catalog = _fetch_nvidia_models()
    except Exception:
        return []

    # Find org prefix (e.g. 'deepseek-ai' from 'deepseek-ai/deepseek-v4-flash')
    if "/" not in full_model:
        return []
    org, model_name = full_model.split("/", 1)
    org = org.lower()
    model_name_lower = model_name.lower()

    # Score models by similarity: same org gets priority, then shared prefix parts
    scored: list[tuple[int, str]] = []
    name_parts = set(model_name_lower.split("-"))

    for short_id, full_id in catalog.items():
        if full_id == full_model:
            continue
        if "/" not in full_id:
            continue
        cat_org, cat_name = full_id.split("/", 1)

        score = 0
        if cat_org.lower() == org:
            score += 10
            # Shared word parts (e.g. 'deepseek', 'v4')
            cat_parts = set(cat_name.lower().split("-"))
            score += len(name_parts & cat_parts)

        if score >= 10:  # At least same org
            scored.append((score, full_id))

    scored.sort(key=lambda x: (-x[0], x[1]))
    return [fid for _, fid in scored[:max_results]]


def resolve_model_name(model: str) -> str:
    """
    Resolve a short model name to full NIM model ID.

    Uses the same logic as config.settings._to_full_nim_model()
    to dynamically look up the org prefix from NVIDIA's model catalog.
    """
    return _to_full_nim_model(model)


async def validate_model_exists(model: str) -> bool:
    """
    Check if model exists in NVIDIA's model catalog.

    Args:
        model: Model ID to check (can be short name like 'deepseek-v4-pro'
               or full ID like 'deepseek-ai/deepseek-v4-pro')

    Returns:
        True if model exists in catalog, False otherwise
    """
    # Resolve short name to full ID
    full_model = resolve_model_name(model)
    logger.info("SWAPPER_VALIDATE: checking model '{}' resolved to '{}'", model, full_model)

    async def _check():
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(NVIDIA_MODELS_URL)
            resp.raise_for_status()
            data = resp.json()

            # NVIDIA returns OpenAI-compatible model list format
            models = data.get("data", [])
            found = any(m.get("id") == full_model for m in models)
            logger.info("SWAPPER_VALIDATE: model '{}' exists={} (catalog has {} models)", full_model, found, len(models))
            return found

    try:
        return await _execute_with_retry(_check)
    except httpx.HTTPStatusError as e:
        logger.error("SWAPPER_VALIDATE: HTTP error after retries: {} {}", e.response.status_code, e)
        return False
    except (httpx.RequestError, httpx.TimeoutException, asyncio.TimeoutError) as e:
        logger.error("SWAPPER_VALIDATE: network/timeout error after retries: {}", e)
        return False
    except Exception as e:
        logger.error("SWAPPER_VALIDATE: exception type={} error={}", type(e).__name__, e)
        return False


async def test_model(model: str, settings: Settings, api_key: str) -> bool:
    """
    Test a model with a minimal request to verify it works.

    Args:
        model: Model ID to test (will be resolved to full NIM ID)
        settings: Application settings
        api_key: API key for authentication

    Returns:
        True if test request succeeds (2xx), False otherwise
    """
    # Resolve short name to full NIM ID
    full_model = resolve_model_name(model)

    async def _test():
        # Create a minimal OpenAI-format request body
        # NOTE: No thinking/reasoning params - this is a connectivity test only.
        test_prompt = settings.swapper_test_prompt
        messages = [{"role": "user", "content": test_prompt}]

        headers = {
            "Authorization": f"Bearer {settings.api_key}",
            "Content-Type": "application/json",
        }

        body = {
            "model": full_model,
            "messages": messages,
            "max_tokens": 10,
            "temperature": 0,
            "stream": False,
        }

        url = f"{NVIDIA_NIM_BASE_URL}/chat/completions"
        logger.info("SWAPPER_TEST: POST {} model={}", url, full_model)
        async with httpx.AsyncClient(timeout=settings.swapper_test_timeout) as client:
            resp = await client.post(url, headers=headers, json=body)
            logger.info("SWAPPER_TEST: status={} body_length={}", resp.status_code, len(resp.content))
            if not resp.is_success:
                logger.warning("SWAPPER_TEST: failed status={} body={}", resp.status_code, resp.text[:500])
                # Raise HTTPStatusError for retryable status codes
                if resp.status_code in _RETRYABLE_STATUS:
                    resp.raise_for_status()
            return resp.is_success

    try:
        return await _execute_with_retry(_test)
    except httpx.HTTPStatusError as e:
        logger.error("SWAPPER_TEST: HTTP error after retries: {} {}", e.response.status_code, e)
        return False
    except (httpx.RequestError, httpx.TimeoutException, asyncio.TimeoutError) as e:
        logger.error("SWAPPER_TEST: network/timeout error after retries: {}", e)
        return False
    except Exception as e:
        logger.error("SWAPPER_TEST: exception type={} error={}", type(e).__name__, e)
        return False


async def validate_and_test_model(
    model: str, settings: Settings, api_key: str
) -> tuple[bool, str]:
    """
    Validate model exists and test it works.

    Returns:
        Tuple of (success: bool, message: str)
    """
    # Step 1: Check model exists in catalog
    exists = await validate_model_exists(model)
    if not exists:
        full_model = resolve_model_name(model)
        similar = _find_similar_models(full_model)
        base_msg = f"Model '{model}' isn't available at the time"
        if similar:
            suggestions = ", ".join(similar[:5])
            return False, f"{base_msg}. You could try: {suggestions}"
        return False, base_msg

    # Step 2: Test model with minimal request
    works = await test_model(model, settings, api_key)
    if not works:
        full_model = resolve_model_name(model)
        similar = _find_similar_models(full_model)
        base_msg = f"Model '{model}' isn't available at the time"
        if similar:
            suggestions = ", ".join(similar[:5])
            return False, f"{base_msg}. You could try: {suggestions}"
        return False, base_msg

    return True, f"Model '{model}' validated and tested successfully"
