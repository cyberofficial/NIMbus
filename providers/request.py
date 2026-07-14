"""Request builder for NVIDIA NIM provider."""

from typing import Any

from loguru import logger

from config.nim import NimSettings
from config.settings import get_reasoning_config
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

    # Determine model for reasoning config (use original_model if available)
    model = getattr(request_data, "original_model", None) or body.get("model", "")

    # Extract thinking params from Anthropic request (Claude 3.7+)
    request_effort = None
    request_budget = None
    thinking = getattr(request_data, "thinking", None)
    # Check if thinking is a proper object/dict (not just a truthy MagicMock)
    if isinstance(thinking, dict):
        request_effort = thinking.get("effort")
        request_budget = thinking.get("budget_tokens")
    elif thinking is not None and not isinstance(thinking, type(None).__class__):
        # Check if it's a proper object with expected attributes (not MagicMock)
        try:
            request_effort = getattr(thinking, "effort", None)
            request_budget = getattr(thinking, "budget_tokens", None)
        except AttributeError:
            pass

    # Get model-specific reasoning config (for max budget, effort mapping)
    reasoning_config = get_reasoning_config(model)

    # Handle thinking/reasoning mode - only when NIM_THINKING is enabled
    if nim.thinking and nim.enable_thinking:
        # Exact <nimeffort:level> tag in last user message (exact match like <nimrpm:reset>)
        last_user_msg = None
        for msg in reversed(getattr(request_data, "messages", [])):
            if getattr(msg, "role", None) == "user":
                msg_text = ""
                if isinstance(msg.content, str):
                    msg_text = msg.content
                elif isinstance(msg.content, list):
                    for block in msg.content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            msg_text += block.get("text", "")
                        elif hasattr(block, "text"):
                            msg_text += getattr(block, "text", "")
                if msg_text:
                    last_user_msg = msg_text
                    break

    # Check for exact <nimeffort:level> tag (exact match like <nimrpm:reset>)
    exact_effort_tag = None
    if last_user_msg:
        from api.swapper.parser import extract_nimeffort_tag, is_nimeffort_tag

        if is_nimeffort_tag(last_user_msg):
            exact_effort_tag = extract_nimeffort_tag(last_user_msg)

    # Get model-specific reasoning config (for max budget, effort mapping)
    reasoning_config = get_reasoning_config(model)

    # Handle thinking/reasoning mode - only when NIM_THINKING is enabled
    if nim.thinking and nim.enable_thinking:
        # Map effort: exact tag > request > session cache > settings > default (high)
        exact_tag_effort = exact_effort_tag if exact_effort_tag else None
        effective_effort = exact_tag_effort or request_effort or nim.reasoning_effort

        # Remove session cache code - no more session caching for effort

        # Get model-specific effort mapping
        effort_map = reasoning_config.effort_mapping if reasoning_config.effort_mapping else {}
        model_effort = effort_map.get(effective_effort, effective_effort)

        # Build chat_template_kwargs using MAPPED effort
        ctk = extra_body.setdefault("chat_template_kwargs", {})
        ctk["enable_thinking"] = nim.chat_template_enable_thinking

        # Set chat_template_kwargs effort flags using mapped effort
        if model_effort == "low":
            ctk["low_effort"] = True
        elif model_effort == "medium":
            ctk["medium_effort"] = True
        elif model_effort == "high":
            ctk["high_effort"] = True

        # reasoning_budget: request > settings > budget_per_effort (based on effort) > model max
        effective_budget = request_budget
        if effective_budget is None:
            if nim.reasoning_budget > 0:
                effective_budget = nim.reasoning_budget
            else:
                # Use budget_per_effort based on original effort (before mapping)
                effort_for_budget = effective_effort.lower()
                effective_budget = reasoning_config.budget_per_effort.get(
                    effort_for_budget,
                    reasoning_config.max_reasoning_budget
                )

        # Auto-clamp to model max
        if effective_budget > reasoning_config.max_reasoning_budget:
            logger.warning(
                "reasoning_budget {} exceeds model max {} for {}, clamping",
                effective_budget, reasoning_config.max_reasoning_budget, model
            )
            effective_budget = reasoning_config.max_reasoning_budget

        if effective_budget > 0 and reasoning_config.supports_thinking:
            _set_extra(extra_body, "reasoning_budget", effective_budget)

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
    if nim.thinking and nim.enable_thinking:
        _set_extra(extra_body, "return_tokens_as_token_ids", nim.return_tokens_as_token_ids)
    _set_extra(extra_body, "include_stop_str_in_output", nim.include_stop_str_in_output)
    _set_extra(extra_body, "ignore_eos", nim.ignore_eos)

    if extra_body:
        body["extra_body"] = extra_body

    logger.debug(
        "NIM_REQUEST: conversion done model={} msgs={} tools={}",
        body.get("model"),
        len(body.get("messages", [])),
        len(body.get("tools", [])),
    )
    return body
