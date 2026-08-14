"""Hidden auto-compaction for context length errors.

When the provider hits a context length limit (400 BadRequestError with
"maximum context length"), instead of failing, this module:
1. Summarizes the conversation history (excluding the last user message)
2. Replaces old messages with summary + retained recent messages
3. Retries the original request transparently
4. Recursively handles multiple compaction layers (depth-capped)
"""

import json
import re
import uuid
from typing import Any, AsyncIterator, List, Optional, Tuple

from loguru import logger
from openai import BadRequestError, UnprocessableEntityError, APIStatusError

from config.nim import NimSettings
from providers.provider import NvidiaNimProvider
from providers.request_queue import RequestPriority
from api.request_utils import get_token_count
from providers.sse_builder import SSEBuilder
from providers.think_parser import ContentType


# Context length error patterns
CONTEXT_LENGTH_PATTERNS = [
    "maximum context length",
    "context length exceeded",
    "context window exceeded",
    "exceeds maximum context",
    "too many tokens",
    "token limit",
]


def _is_context_length_error(e: Exception) -> bool:
    """Check if error is a context length exceeded error."""
    error_msg = str(e).lower()
    for pattern in CONTEXT_LENGTH_PATTERNS:
        if pattern in error_msg:
            return True
    if hasattr(e, "body") and isinstance(e.body, dict):
        msg = e.body.get("message", "").lower()
        for pattern in CONTEXT_LENGTH_PATTERNS:
            if pattern in msg:
                return True
    return False


def _extract_token_counts(e: Exception) -> Tuple[Optional[int], Optional[int]]:
    """Extract (max_tokens, actual_tokens) from error if available."""
    if hasattr(e, "body") and isinstance(e.body, dict):
        msg = e.body.get("message", "")
        max_match = re.search(r"maximum context length is (\d+)", msg)
        actual_match = re.search(r"resulted in (\d+)", msg)
        max_tokens = int(max_match.group(1)) if max_match else None
        actual_tokens = int(actual_match.group(1)) if actual_match else None
        return max_tokens, actual_tokens
    return None, None


def _get_max_context_tokens(e: Exception, default: int = 200_000) -> int:
    """Get maximum allowed context tokens from error or a safe default."""
    max_tokens, _ = _extract_token_counts(e)
    if max_tokens is not None and max_tokens > 0:
        return int(max_tokens * 0.85)
    return default


async def _generate_summary(
    provider: NvidiaNimProvider,
    messages: List[dict],
    nim_settings: NimSettings,
    model: str,
    request_id: str,
) -> str:
    """Generate a summary of the conversation using the provider."""
    conversation_text = "\n\n".join(
        f"{m['role'].capitalize()}: {m['content'][:500]}"
        for m in messages
    )
    summary_prompt = (
        "Please summarize the following conversation concisely, "
        "preserving key context and decisions:\n\n"
        f"{conversation_text[:8000]}"
        "\n\nSummary:"
    )

    from providers.request import build_request_body
    from api.models.anthropic import MessagesRequest, Message

    msg_request = MessagesRequest(
        model=model,
        messages=[Message(role="user", content=summary_prompt)],
        max_tokens=2000,
    )
    body = build_request_body(msg_request, nim_settings, model_override=model)
    body["stream"] = True
    input_tokens = get_token_count(msg_request.messages, None, None)

    summary_text = ""
    stream = provider.stream_response(
        msg_request, input_tokens, request_id=request_id, priority=RequestPriority.HIGH
    )
    try:
        async for chunk in stream:
            if not chunk.strip():
                continue
            try:
                event_data = chunk.split("data: ", 1)[-1].strip()
                data = json.loads(event_data)
                if data.get("type") == "content_block_delta":
                    delta = data.get("delta", {})
                    if delta.get("type") == "text_delta":
                        summary_text += delta.get("text", "")
            except Exception:
                continue
    except Exception as e:
        logger.error(f"Hidden compact: Summary generation failed: {e}")
        summary_text = "[Summary generation failed]"

    return summary_text


def _drop_oldest_until_fits(
    messages: list,
    max_context_tokens: int,
    max_summary_request_tokens: int,
    tag: str,
) -> list:
    """Drop oldest messages until rest fits within context budget.

    ``messages`` items are dicts with ``"content"`` key.
    Token counting uses tiktoken directly on the raw content strings.
    """
    from providers.tiktoken_cache import get_encoder
    _encoder = get_encoder("cl100k_base")

    def _count_dict_msgs(msgs: list) -> int:
        total = 0
        for m in msgs:
            content = m.get("content", "")
            if isinstance(content, str):
                total += len(_encoder.encode(content))
            elif isinstance(content, list):
                for block in content:
                    text = block.get("text", "") if isinstance(block, dict) else str(block)
                    total += len(_encoder.encode(text))
        return total

    while messages:
        token_count = _count_dict_msgs(messages)
        if token_count + max_summary_request_tokens <= max_context_tokens:
            break
        messages.pop(0)
        logger.debug(f"{tag}: Dropped oldest message (now {len(messages)} messages)")
    return messages


async def _attempt_hidden_compact_streaming(
    provider,
    original_body: dict,
    sse,
    think_parser: Any,
    heuristic_parser: Any,
    channel_id: int,
    conversation_manager: Any,
    tag: str,
    attempt: int,
    usage_info: Any,
    last_error_tag: str,
    original_error: Exception,
    *,
    max_compact_depth: int = 3,
    depth: int = 0,
) -> AsyncIterator[str]:
    """Attempt hidden compaction for streaming with recursive depth cap."""
    from providers.provider import request_id_var
    from providers.sse_builder import map_stop_reason

    nim_settings = provider._nim_settings
    model = original_body.get("model", "")
    original_messages = original_body.get("messages", [])

    if not original_messages:
        logger.warning(f"{tag}_STREAM: No messages to compact")
        raise RuntimeError("No messages to compact")

    if len(original_messages) < 2:
        logger.warning(f"{tag}_STREAM: Not enough messages to compact")
        raise RuntimeError("Not enough messages to compact")

    if depth >= max_compact_depth:
        logger.warning(f"{tag}_STREAM: Max compact depth {depth} reached")
        raise RuntimeError("Hidden compact: max recursion depth exceeded")

    logger.info(
        f"🔄 {tag}_STREAM: Hidden compact layer {depth} — "
        f"summarizing {len(original_messages)} messages..."
    )

    max_context_tokens = _get_max_context_tokens(original_error)
    max_summary_request_tokens = 8000 + 2000

    to_summarize = original_messages[:-1]
    to_retain = original_messages[-1:]

    to_summarize = _drop_oldest_until_fits(
        to_summarize, max_context_tokens, max_summary_request_tokens, f"{tag}_STREAM"
    )

    summary_request_id = f"hidden_compact_{uuid.uuid4().hex[:8]}_d{depth}"
    summary = await _generate_summary(
        provider, to_summarize, nim_settings, model, summary_request_id
    )

    new_messages = [
        {"role": "assistant", "content": f"[Previous conversation summary]: {summary}"}
    ] + to_retain

    if conversation_manager and channel_id:
        try:
            retained_for_cm = [
                {"role": m["role"], "content": m["content"]}
                for m in to_retain
            ]
            if not conversation_manager.was_conversation_reset(channel_id):
                conversation_manager.replace_with_compacted(
                    channel_id,
                    f"[Previous conversation summary]: {summary}",
                    retained_for_cm,
                )
        except Exception as e:
            logger.warning(f"{tag}_STREAM: Failed to update conversation manager: {e}")

    logger.info(
        f"🔄 {tag}_STREAM: Compact layer {depth} done — "
        f"{len(original_messages)} -> {len(new_messages)} msgs"
    )

    new_body = original_body.copy()
    new_body["messages"] = new_messages

    request_id = original_body.get("request_id", f"retry_{uuid.uuid4().hex[:8]}")
    req_token = request_id_var.set(request_id)

    try:
        stream = await provider._global_rate_limiter.execute_with_retry(
            provider._client.chat.completions.create,
            **new_body,
            stream=True,
            use_worker_slot=False,
            max_retries=provider._config.retry_on_truncation,
        )
    except Exception as retry_e:
        if _is_context_length_error(retry_e) and depth + 1 < max_compact_depth:
            logger.info(f"🔄 {tag}_STREAM: Recursing to layer {depth + 1}")
            async for event in _attempt_hidden_compact_streaming(
                provider=provider,
                original_body=new_body,
                sse=sse,
                think_parser=think_parser,
                heuristic_parser=heuristic_parser,
                channel_id=channel_id,
                conversation_manager=conversation_manager,
                tag=tag,
                attempt=attempt,
                usage_info=usage_info,
                last_error_tag=last_error_tag,
                original_error=retry_e,
                max_compact_depth=max_compact_depth,
                depth=depth + 1,
            ):
                yield event
            return
        raise
    finally:
        request_id_var.reset(req_token)

    try:
        async for chunk in stream:
            if getattr(chunk, "usage", None):
                usage_info = chunk.usage
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            delta = choice.delta
            if delta is None:
                continue

            # Handle reasoning content
            reasoning = getattr(delta, "reasoning_content", None)
            if reasoning:
                for event in sse.ensure_thinking_block():
                    yield event
                yield sse.emit_thinking_delta(reasoning)

            # Handle text content
            if delta.content:
                for part in think_parser.feed(delta.content):
                    if part.type == ContentType.THINKING:
                        for event in sse.ensure_thinking_block():
                            yield event
                        yield sse.emit_thinking_delta(part.content)
                    else:
                        filtered_text, _ = heuristic_parser.feed(part.content)
                        if filtered_text:
                            for event in sse.ensure_text_block():
                                yield event
                            yield sse.emit_text_delta(filtered_text)

            # Check for stop
            if choice.finish_reason:
                stop_reason = map_stop_reason(choice.finish_reason)
                for event in sse.close_content_blocks():
                    yield event
                remaining = think_parser.flush()
                if remaining is not None:
                    if remaining.type == ContentType.THINKING:
                        for event in sse.ensure_thinking_block():
                            yield event
                        yield sse.emit_thinking_delta(remaining.content)
                yield sse.message_delta(stop_reason, 0)
                yield sse.message_stop()
                break
    except Exception as e:
        logger.error(f"{tag}_STREAM: Hidden compact retry failed: {e}")
        raise


async def _attempt_hidden_compact_buffered(
    provider,
    original_body: dict,
    channel_id: int,
    conversation_manager: Any,
    tag: str,
    attempt: int,
    original_error: Exception,
    *,
    max_compact_depth: int = 3,
    depth: int = 0,
) -> dict:
    """Attempt hidden compaction for buffered requests with recursive depth cap."""
    from providers.provider import request_id_var

    nim_settings = provider._nim_settings
    model = original_body.get("model", "")
    original_messages = original_body.get("messages", [])

    if not original_messages:
        logger.warning(f"{tag}_BUFFERED: No messages to compact")
        raise RuntimeError("No messages to compact")

    if len(original_messages) < 2:
        logger.warning(f"{tag}_BUFFERED: Not enough messages to compact")
        raise RuntimeError("Not enough messages to compact")

    if depth >= max_compact_depth:
        logger.warning(f"{tag}_BUFFERED: Max compact depth {depth} reached")
        raise RuntimeError("Hidden compact: max recursion depth exceeded")

    logger.info(
        f"🔄 {tag}_BUFFERED: Hidden compact layer {depth} — "
        f"summarizing {len(original_messages)} messages..."
    )

    max_context_tokens = _get_max_context_tokens(original_error)
    max_summary_request_tokens = 8000 + 2000

    to_summarize = original_messages[:-1]
    to_retain = original_messages[-1:]

    to_summarize = _drop_oldest_until_fits(
        to_summarize, max_context_tokens, max_summary_request_tokens, f"{tag}_BUFFERED"
    )

    summary_request_id = f"hidden_compact_{uuid.uuid4().hex[:8]}_d{depth}"
    summary = await _generate_summary(
        provider, to_summarize, nim_settings, model, summary_request_id
    )

    new_messages = [
        {"role": "assistant", "content": f"[Previous conversation summary]: {summary}"}
    ] + to_retain

    if conversation_manager and channel_id:
        try:
            retained_for_cm = [
                {"role": m["role"], "content": m["content"]}
                for m in to_retain
            ]
            if not conversation_manager.was_conversation_reset(channel_id):
                conversation_manager.replace_with_compacted(
                    channel_id,
                    f"[Previous conversation summary]: {summary}",
                    retained_for_cm,
                )
        except Exception as e:
            logger.warning(f"{tag}_BUFFERED: Failed to update conversation manager: {e}")

    logger.info(
        f"🔄 {tag}_BUFFERED: Compact layer {depth} done — "
        f"{len(original_messages)} -> {len(new_messages)} msgs"
    )

    new_body = original_body.copy()
    new_body["messages"] = new_messages

    request_id = original_body.get("request_id", f"retry_{uuid.uuid4().hex[:8]}")
    req_token = request_id_var.set(request_id)

    try:
        response = await provider._global_rate_limiter.execute_with_retry(
            provider._client.chat.completions.create,
            **new_body,
            stream=False,
        )
    except Exception as retry_e:
        if _is_context_length_error(retry_e) and depth + 1 < max_compact_depth:
            logger.info(f"🔄 {tag}_BUFFERED: Recursing to layer {depth + 1}")
            
            return await _attempt_hidden_compact_buffered(
                provider=provider,
                original_body=new_body,
                channel_id=channel_id,
                conversation_manager=conversation_manager,
                tag=tag,
                attempt=attempt,
                original_error=retry_e,
                max_compact_depth=max_compact_depth,
                depth=depth + 1,
            )
        raise
    finally:
        request_id_var.reset(req_token)

    result = provider._build_anthropic_response(response, None, 0)
    logger.info(f"{tag}_BUFFERED: Hidden compact retry success")
    return result
