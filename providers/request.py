"""Request builder for NVIDIA NIM provider."""

from typing import Any

from loguru import logger

from config.nim import NimSettings
from providers.message_converter import build_base_request_body
from providers.utils import set_if_not_none


def _set_extra(
    extra_body: dict[str, Any], key: str, value: Any, ignore_value: Any = None
) -> None:
    if key in extra_body:
        return
    if value is None:
        return
    if ignore_value is not None and value == ignore_value:
        return
    extra_body[key] = value


def build_request_body(
    request_data: Any, nim: NimSettings, *, system_as_user: bool = False
) -> dict:
    """Build OpenAI-format request body from Anthropic request.

    Args:
        request_data: The Anthropic-format request.
        nim: NIM settings for parameters like max_tokens, temperature, etc.
        system_as_user: When True, system prompts are placed as user messages
            (for models that don't support the system role).
    """
    logger.debug(
        "NIM_REQUEST: conversion start model={} msgs={}",
        getattr(request_data, "model", "?"),
        len(getattr(request_data, "messages", [])),
    )
    body = build_base_request_body(request_data, system_as_user=system_as_user)

    # NIM-specific max_tokens: cap against nim.max_tokens
    max_tokens = body.get("max_tokens") or getattr(request_data, "max_tokens", None)
    if max_tokens is None:
        max_tokens = nim.max_tokens
    elif nim.max_tokens:
        max_tokens = min(max_tokens, nim.max_tokens)
    set_if_not_none(body, "max_tokens", max_tokens)

    # NIM-specific temperature/top_p: fall back to NIM defaults if request didn't set
    if body.get("temperature") is None and nim.temperature is not None:
        body["temperature"] = nim.temperature
    if body.get("top_p") is None and nim.top_p is not None:
        body["top_p"] = nim.top_p

    # NIM-specific stop sequences fallback
    if "stop" not in body and nim.stop:
        body["stop"] = nim.stop

    if nim.presence_penalty != 0.0:
        body["presence_penalty"] = nim.presence_penalty
    if nim.frequency_penalty != 0.0:
        body["frequency_penalty"] = nim.frequency_penalty
    if nim.seed is not None:
        body["seed"] = nim.seed

    body["parallel_tool_calls"] = nim.parallel_tool_calls

    # Handle non-standard parameters via extra_body
    extra_body: dict[str, Any] = {}
    request_extra = getattr(request_data, "extra_body", None)
    if request_extra:
        extra_body.update(request_extra)

    # Handle thinking/reasoning mode - only when NIM_THINKING is enabled
    if nim.thinking:
        extra_body.setdefault("thinking", {"type": "enabled"})
        extra_body.setdefault("reasoning_split", True)
        extra_body.setdefault(
            "chat_template_kwargs",
            {
                "thinking": True,
                "enable_thinking": True,
                "reasoning_split": True,
                "clear_thinking": False,
            },
        )

    req_top_k = getattr(request_data, "top_k", None)
    top_k = req_top_k if req_top_k is not None else nim.top_k
    _set_extra(extra_body, "top_k", top_k, ignore_value=-1)
    _set_extra(extra_body, "min_p", nim.min_p, ignore_value=0.0)
    _set_extra(
        extra_body, "repetition_penalty", nim.repetition_penalty, ignore_value=1.0
    )
    _set_extra(extra_body, "min_tokens", nim.min_tokens, ignore_value=0)
    _set_extra(extra_body, "chat_template", nim.chat_template)
    _set_extra(extra_body, "request_id", nim.request_id)
    if nim.thinking:
        _set_extra(extra_body, "return_tokens_as_token_ids", nim.return_tokens_as_token_ids)
    _set_extra(extra_body, "include_stop_str_in_output", nim.include_stop_str_in_output)
    _set_extra(extra_body, "ignore_eos", nim.ignore_eos)
    if nim.thinking:
        # Get effort level from request (Claude Code's /effort command) or fallback to config
        request_effort = getattr(request_data.thinking, 'effort', None) if request_data.thinking else None
        effective_effort = request_effort or nim.reasoning_effort

        # Get model-specific effort mapping
        # Use original_model (before mapping) for pattern matching against user-defined mappings
        original_model = getattr(request_data, "original_model", None) or body.get("model", "")
        effort_map = nim.get_effort_map_for_model(original_model)

        # Map Claude effort level to model-specific value if mapping exists
        model_effort = effort_map.get(effective_effort, effective_effort)
        _set_extra(extra_body, "reasoning_effort", model_effort)
        _set_extra(extra_body, "include_reasoning", nim.include_reasoning)

    if extra_body:
        body["extra_body"] = extra_body

    logger.debug(
        "NIM_REQUEST: conversion done model={} msgs={} tools={}",
        body.get("model"),
        len(body.get("messages", [])),
        len(body.get("tools", [])),
    )
    return body
