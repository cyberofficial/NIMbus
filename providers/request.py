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
    request_data: Any, nim: NimSettings, *, system_as_user: bool = False, session_id: str | None = None, model_override: str | None = None
) -> dict:
    """Build OpenAI-format request body from Anthropic request.

    Args:
        request_data: The Anthropic-format request.
        nim: NIM settings for parameters like max_tokens, temperature, etc.
        system_as_user: When True, system prompts are placed as user messages
            (for models that don't support the system role).
        session_id: Optional session ID for per-session effort/budget settings.
        model_override: Optional model name to override for thinking config lookup
            (used when model swap is active, so thinking params match the swapped model).
    """
    logger.debug(
        "NIM_REQUEST: conversion start model={} msgs={}",
        getattr(request_data, "model", "?"),
        len(getattr(request_data, "messages", [])),
    )
    body = build_base_request_body(request_data, system_as_user=system_as_user)

    # max_tokens: use request value if provided, otherwise don't set (let model use defaults)
    max_tokens = body.get("max_tokens") or getattr(request_data, "max_tokens", None)
    # Upgrade max_tokens against NIM_MAX_TOKENS if configured (0 = no upgrade)
    if max_tokens is not None and nim.max_tokens and nim.max_tokens > 0:
        if max_tokens < nim.max_tokens:
            logger.info("max_tokens {} upgraded to NIM_MAX_TOKENS: {}", max_tokens, nim.max_tokens)
            max_tokens = nim.max_tokens
    if max_tokens is not None:
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

    # Determine model for reasoning config - use model_override if provided
    # (for model swap scenarios), otherwise use the resolved NIM model name.
    # This ensures thinking params match the actual model being called.
    if model_override:
        model = model_override
    else:
        body_model = body.get("model", "")
        model = body_model or getattr(request_data, "original_model", None) or getattr(request_data, "model", "")

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

    # Get model-specific reasoning config (for max budget, effort mapping, thinking style)
    reasoning_config = get_reasoning_config(model)

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
    custom_budget_from_tag = None
    if last_user_msg:
        from api.swapper.parser import extract_nimeffort_tag, is_nimeffort_tag

        if is_nimeffort_tag(last_user_msg):
            effort_level_from_tag, budget_from_tag = extract_nimeffort_tag(last_user_msg)
            # Handle combined format: <nimeffort:ultracode:100>
            if effort_level_from_tag and budget_from_tag is not None:
                exact_effort_tag = effort_level_from_tag
                custom_budget_from_tag = budget_from_tag
            # Handle budget-only: <nimeffort:100>
            elif budget_from_tag is not None:
                custom_budget_from_tag = budget_from_tag
            # Handle level-only: <nimeffort:ultracode>
            elif effort_level_from_tag:
                exact_effort_tag = effort_level_from_tag

    # Handle thinking/reasoning mode - only when NIM_THINKING enabled AND model supports thinking
    if nim.thinking and reasoning_config.supports_thinking:
        # Get session-stored named effort and custom budget
        session_effort = None
        session_custom_budget = None
        if session_id:
            try:
                from api.effort_store import get_effort_level, get_effort_budget
                session_effort = get_effort_level(session_id)
                session_custom_budget = get_effort_budget(session_id)
            except Exception:
                pass

        # Map effort: exact tag > request > session > settings > default (high)
        exact_tag_effort = exact_effort_tag if exact_effort_tag else None
        effective_effort = exact_tag_effort or request_effort or session_effort or nim.reasoning_effort

        # Get model-specific effort mapping
        effort_map = reasoning_config.effort_mapping if reasoning_config.effort_mapping else {}
        model_effort = effort_map.get(effective_effort, effective_effort)

        # Apply thinking params based on thinking_style
        thinking_style = reasoning_config.thinking_style

        if thinking_style == "nemotron":
            # Nemotron: chat_template_kwargs.enable_thinking + reasoning_budget
            ctk = extra_body.setdefault("chat_template_kwargs", {})
            ctk["enable_thinking"] = nim.chat_template_enable_thinking
            # reasoning_budget handled below

        elif thinking_style == "deepseek":
            # DeepSeek: chat_template_kwargs.thinking + reasoning_effort
            ctk = extra_body.setdefault("chat_template_kwargs", {})
            ctk["thinking"] = True  # Enable thinking when enabled globally
            ctk["reasoning_effort"] = model_effort

        elif thinking_style == "minimax":
            # MinMax: chat_template_kwargs.thinking_mode (enabled/adaptive/disabled)
            # Use effort_mapping from config directly
            ctk = extra_body.setdefault("chat_template_kwargs", {})
            model_effort = effort_map.get(effective_effort, effective_effort)
            ctk["thinking_mode"] = model_effort

        else:
            # Default style: current behavior with effort flags
            ctk = extra_body.setdefault("chat_template_kwargs", {})
            ctk["enable_thinking"] = nim.chat_template_enable_thinking

            if model_effort == "low":
                ctk["low_effort"] = True
            elif model_effort == "medium":
                ctk["medium_effort"] = True
            elif model_effort == "high":
                ctk["high_effort"] = True

        # reasoning_budget: request > numeric tag > session custom budget > settings > budget_per_effort
        effective_budget = request_budget
        if effective_budget is None:
            if custom_budget_from_tag is not None:
                effective_budget = custom_budget_from_tag
            elif session_custom_budget is not None:
                effective_budget = session_custom_budget
            elif nim.reasoning_budget == -1:
                effective_budget = -1  # unlimited - pass through to API
            elif nim.reasoning_budget > 0:
                effective_budget = nim.reasoning_budget
            else:
                # Use budget_per_effort based on original effort (before mapping)
                effort_for_budget = effective_effort.lower()
                effective_budget = reasoning_config.budget_per_effort.get(
                    effort_for_budget,
                    reasoning_config.max_reasoning_budget
                )

        # Set max_tokens from budget so the model can use its full reasoning capability
        # (e.g. deepseek-v4-flash ultracode=384k sets max_tokens=384000)
        if effective_budget > 0:
            body["max_tokens"] = effective_budget

        # Add reasoning_budget for nemotron and default styles (nemotron needs it, default for backward compat)
        # DeepSeek does NOT support reasoning_budget - it uses reasoning_effort in chat_template_kwargs instead
        if thinking_style in ("nemotron", "default"):
            if effective_budget > 0:
                _set_extra(extra_body, "reasoning_budget", effective_budget)
            elif effective_budget == -1:
                _set_extra(extra_body, "reasoning_budget", -1)  # unlimited

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
    # DeepSeek-V4 emits tool calls as DSML markup; when its server-side
    # tool-call parser fails on degraded tokens, the markup is stripped from
    # content unless stop strings are included, leaving nothing to recover.
    if model.startswith("deepseek-ai/deepseek-v4"):
        _set_extra(extra_body, "include_stop_str_in_output", True)
    else:
        _set_extra(extra_body, "include_stop_str_in_output", nim.include_stop_str_in_output)
    _set_extra(extra_body, "ignore_eos", nim.ignore_eos)

    if extra_body:
        body["extra_body"] = extra_body

    # Log resolved thinking/reasoning config so operators can see at a glance
    # which thinking_style/effort/budget the proxy applied for this model.
    if not nim.thinking:
        # Global kill switch - no thinking params sent regardless of model
        logger.info(
            "THINKING: {} thinking disabled (NIM_THINKING=false, sending no reasoning params)",
            model,
        )
    elif not reasoning_config.supports_thinking:
        # Model not in config (or supports_thinking=false) - let NVIDIA server's defaults apply
        logger.info(
            "THINKING: {} no thinking params (model not in reasoning_config.json - "
            "NVIDIA server defaults will apply)",
            model,
        )
    else:
        # Thinking enabled, model has reasoning config - log the resolved style + effort + budget
        ctk = body.get("extra_body", {}).get("chat_template_kwargs", {})
        budget = body.get("extra_body", {}).get("reasoning_budget")
        budget_str = f" budget={budget}" if budget is not None else ""
        logger.info(
            "THINKING: {} using style={} effort={} -> {}{}",
            model,
            reasoning_config.thinking_style,
            effective_effort,
            model_effort,
            budget_str,
        )

    logger.debug(
        "NIM_REQUEST: conversion done model={} msgs={} tools={}",
        body.get("model"),
        len(body.get("messages", [])),
        len(body.get("tools", [])),
    )
    return body
