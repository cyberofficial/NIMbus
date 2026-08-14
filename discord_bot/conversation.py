"""Conversation management with token tracking for Discord bot."""

import asyncio
import hashlib
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from providers.tiktoken_cache import get_encoder


@dataclass
class ConversationMessage:
    """A single message with user context."""
    role: str
    content: str
    user_id: Optional[int] = None
    username: str = ""  # Display name for context
    reply_message: str = ""  # Content of the bot message this user message is replying to
    tools_used: list[str] = field(default_factory=list)  # List of tool names used in this response
    tool_results: dict | None = None  # Optional: captured tool results for context persistence


@dataclass
class ConversationSession:
    """Session state for a Discord channel conversation."""
    channel_id: int
    messages: List[ConversationMessage] = field(default_factory=list)
    token_count: int = 0
    created_at: float = field(default_factory=time.monotonic)
    last_activity: float = field(default_factory=time.monotonic)
    processing_queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    is_processing: bool = False
    compaction_warning_shown: bool = False  # Track if we warned about auto-compact
    # Fingerprints of messages *before* the most recent compaction/clear,
    # so hidden_compact can detect whether the user has manually reset the conversation.
    previous_fingerprint: str = ""  # SHA-256 hex of concatenated message contents
    compacted_message_count: int = 0  # How many messages have been compacted so far


class ConversationManager:
    """
    Manage conversations per Discord channel with token-based compaction.

    When conversation exceeds threshold (e.g., 80% of max tokens),
    trigger auto-compaction to summarize and reset.
    """

    def __init__(self, max_tokens: int, compact_threshold: float = 0.8, system_prompt: str = ""):
        self._max_tokens = max_tokens
        self._compact_threshold = compact_threshold
        self._encoder = get_encoder("cl100k_base")
        self._system_prompt = system_prompt
        self._system_prompt_tokens = self._count_tokens(system_prompt) if system_prompt else 0

        # Load persisted sessions
        from .persistence import load_conversations
        loaded = load_conversations()
        self._sessions: Dict[int, ConversationSession] = loaded if loaded else {}

    def _count_tokens(self, text: str) -> int:
        """Count tokens using cl100k_base encoding."""
        return len(self._encoder.encode(text))

    def _compute_fingerprint(self, channel_id: int) -> str:
        """Return a SHA-256 fingerprint of current message content for change detection."""
        session = self._sessions.get(channel_id)
        if not session or not session.messages:
            return ""
        concatenated = "".join(
            f"{m.role}:{m.content}" for m in session.messages
        )
        return hashlib.sha256(concatenated.encode("utf-8")).hexdigest()

    def get_session(self, channel_id: int) -> Optional[ConversationSession]:
        """Get or create a conversation session."""
        if channel_id not in self._sessions:
            self._sessions[channel_id] = ConversationSession(channel_id=channel_id)
        return self._sessions.get(channel_id)

    def get_history(self, channel_id: int) -> List[dict]:
        """Get conversation history formatted for NIM API."""
        from .user_blocking import is_blocked
        session = self.get_session(channel_id)
        if not session:
            return []
        return [
            {"role": m.role, "content": m.content}
            for m in session.messages
            if m.user_id is None or not is_blocked(m.user_id)
        ]

    def get_history_for_nim(self, channel_id: int) -> List[dict]:
        """Get conversation history with user context for NIM API.
        Only prepend username for user messages so the model doesn't learn
        to prefix its own responses with 'NIM:' (which compounds over time).
        """
        from .user_blocking import is_blocked
        session = self.get_session(channel_id)
        if not session:
            return []
        return [
            {
                "role": m.role,
                "content": f"{m.username}: {m.content}" if (m.username and m.role == "user") else m.content
            }
            for m in session.messages
            if m.user_id is None or not is_blocked(m.user_id)
        ]

    def get_token_count(self, channel_id: int) -> int:
        """Get current token count for a conversation (includes system prompt)."""
        session = self._sessions.get(channel_id)
        if not session:
            return 0
        return session.token_count + self._system_prompt_tokens

    def should_compact(self, channel_id: int) -> bool:
        """Check if conversation should be auto-compacted."""
        tokens = self.get_token_count(channel_id)
        return tokens >= (self._max_tokens * self._compact_threshold)

    def should_warn_about_compact(self, channel_id: int) -> tuple[bool, float]:
        """
        Check if we should warn about upcoming auto-compaction.
        Returns (should_warn, percentage).
        Warns at 5% before the threshold (e.g., if threshold is 0.8, warn at 0.75).
        """
        tokens = self.get_token_count(channel_id)
        warning_threshold = self._compact_threshold - 0.05
        warning_threshold = max(0.0, warning_threshold)  # Don't go below 0
        percentage = tokens / self._max_tokens if self._max_tokens > 0 else 0

        session = self._sessions.get(channel_id)
        if not session:
            return False, percentage

        # Only warn once per session unless warning flag is reset
        if session.compaction_warning_shown:
            return False, percentage

        should_warn = percentage >= warning_threshold and percentage < self._compact_threshold
        if should_warn:
            session.compaction_warning_shown = True
        return should_warn, percentage

    def reset_compaction_warning(self, channel_id: int) -> None:
        """Reset the compaction warning flag (called after compaction)."""
        session = self._sessions.get(channel_id)
        if session:
            session.compaction_warning_shown = False

    def add_message(self, channel_id: int, role: str, content: str, auto_compact: bool = True) -> dict:
        """
        Add message and return status.

        Returns: {"status": "ok" | "auto_compact" | "needs_compaction" | "dropped"}
        """
        return self.add_message_with_user(
            channel_id=channel_id, role=role, content=content,
            user_id=None, username="", reply_message="",
            tools_used=[], tool_results=None, auto_compact=auto_compact
        )

    def add_message_with_user(
        self, channel_id: int, role: str, content: str,
        user_id: Optional[int] = None, username: str = "", reply_message: str = "",
        tools_used: list[str] | None = None, tool_results: dict | None = None,
        auto_compact: bool = True
    ) -> dict:
        """
        Add message with user context and return status.

        Args:
            auto_compact: If False and token limit reached, drop oldest messages instead of compacting

        Returns: {"status": "ok" | "auto_compact" | "needs_compaction" | "dropped"}
        """
        msg_tokens = self._count_tokens(content)
        current_tokens = self.get_token_count(channel_id)

        # Get or create session
        session = self.get_session(channel_id)
        if session is None:
            session = ConversationSession(channel_id=channel_id)
            self._sessions[channel_id] = session

        # If auto-compact is disabled, drop oldest messages until we have room
        if not auto_compact:
            # If this single message exceeds the limit, we can't add it
            if msg_tokens > self._max_tokens:
                return {"status": "needs_compaction", "tokens": msg_tokens}

            # Drop oldest messages until we have room
            while session.messages and (session.token_count + msg_tokens) > self._max_tokens:
                # Remove oldest message (index 1 to preserve system/first message if possible, or index 0)
                # Actually we want FIFO, so remove from index 0
                oldest = session.messages.pop(0)
                oldest_tokens = self._count_tokens(oldest.content)
                session.token_count -= oldest_tokens
                if session.token_count < 0:
                    session.token_count = 0

        msg = ConversationMessage(
            role=role, content=content, user_id=user_id, username=username,
            reply_message=reply_message, tools_used=tools_used or [],
            tool_results=tool_results
        )
        session.messages.append(msg)
        session.token_count += msg_tokens
        session.last_activity = time.monotonic()

        # Check if we should auto-compact (only if auto_compact is enabled)
        if auto_compact and self.should_compact(channel_id):
            # Save before returning auto_compact status
            from .persistence import save_conversations
            save_conversations(self._sessions)
            return {"status": "auto_compact", "current_tokens": session.token_count}

        # Persist after every message
        from .persistence import save_conversations
        save_conversations(self._sessions)

        return {"status": "ok"}

    def get_compact_context(self, channel_id: int) -> Tuple[List[dict], int]:
        """
        Get conversation context for compaction.
        Returns (messages, current_token_count).
        """
        session = self._sessions.get(channel_id)
        if not session:
            return [], 0
        return [{"role": m.role, "content": m.content} for m in session.messages], session.token_count

    def clear(self, channel_id: int) -> None:
        """Clear conversation session (for /new command).

        Saves the fingerprint of cleared messages so hidden_compact can
        detect that the user has manually reset the conversation.
        """
        if channel_id in self._sessions:
            # Capture fingerprint before clearing so hidden_compact can
            # detect that the user manually reset the conversation.
            self._sessions[channel_id].previous_fingerprint = self._compute_fingerprint(channel_id)
            del self._sessions[channel_id]
        # Persist the clear
        from .persistence import save_conversations
        save_conversations(self._sessions)

    def compact(self, channel_id: int, summary: str) -> None:
        """
        Replace conversation with summary.
        Called after compaction is complete.
        """
        # Capture fingerprint of pre-compact messages so hidden_compact
        # can detect when the user issues a manual /compact.
        if channel_id in self._sessions:
            self._sessions[channel_id].previous_fingerprint = self._compute_fingerprint(channel_id)
        summary_tokens = self._count_tokens(summary)
        self._sessions[channel_id] = ConversationSession(
            channel_id=channel_id,
            messages=[ConversationMessage(role="assistant", content=summary, username="Summary", reply_message="", tools_used=[], tool_results=None)],
            token_count=summary_tokens,
        )
        # Persist after compaction and reset warning flag
        from .persistence import save_conversations
        save_conversations(self._sessions)

    def replace_with_compacted(
        self,
        channel_id: int,
        summary: str,
        retained_messages: list[dict],
    ) -> None:
        """
        Replace conversation history with a compacted version.

        Called after a hidden auto-compaction succeeds.  Inserts the summary as
        an assistant message, then appends the retained (recent) messages.

        Args:
            channel_id: Discord channel ID.
            summary: Compacted summary of older messages.
            retained_messages: List of ``{"role": ..., "content": ...}`` dicts
                representing recent messages that were kept as-is.
        """
        session = self.get_session(channel_id)
        if session is None:
            session = ConversationSession(channel_id=channel_id)
            self._sessions[channel_id] = session

        # Build new message list: summary + retained messages
        new_messages: list[ConversationMessage] = [
            ConversationMessage(
                role="assistant",
                content=summary,
                username="[Summary]",
            ),
        ]
        for rm in retained_messages:
            new_messages.append(
                ConversationMessage(
                    role=rm.get("role", "user"),
                    content=rm.get("content", ""),
                )
            )

        # Recalculate token count
        new_tokens = sum(self._count_tokens(m.content) for m in new_messages)

        # Store fingerprint of pre-compaction messages for change detection
        session.previous_fingerprint = self._compute_fingerprint(channel_id)
        session.compacted_message_count += 1

        session.messages = new_messages
        session.token_count = new_tokens
        session.last_activity = time.monotonic()
        session.compaction_warning_shown = False  # Reset warning after compact

        # Persist
        from .persistence import save_conversations
        save_conversations(self._sessions)

    def was_conversation_reset(self, channel_id: int) -> bool:
        """Check if the user has manually reset (/new or /compact) since last hidden compaction.

        Returns True when the current messages no longer match the fingerprint
        captured at the time of the most recent compact/clear — meaning the user
        issued a /new or /compact command, so tracked state should be reset.
        """
        session = self._sessions.get(channel_id)
        if not session:
            return True  # No session == definitely reset
        if not session.previous_fingerprint:
            return False  # Never been compacted/cleared
        current_fp = self._compute_fingerprint(channel_id)
        return current_fp != session.previous_fingerprint

    def get_history_with_tool_results(self, channel_id: int, max_tool_result_size: int = 5000) -> List[dict]:
        """
        Get conversation history with tool results injected as context for NIM API.

        Tool results are formatted as system messages or embedded in assistant messages
        to provide context for subsequent turns.

        Args:
            channel_id: The Discord channel ID
            max_tool_result_size: Max characters per tool result to include

        Returns:
            List of message dicts for NIM API with tool results embedded
        """
        from .user_blocking import is_blocked
        session = self.get_session(channel_id)
        if not session:
            return []

        results = []
        for m in session.messages:
            if m.user_id is not None and is_blocked(m.user_id):
                continue

            # Add the main message
            content = f"{m.username}: {m.content}" if (m.username and m.role == "user") else m.content
            results.append({"role": m.role, "content": content})

            # Inject tool results as additional context if present
            if m.tool_results:
                for tool_name, tool_data in m.tool_results.items():
                    result_text = tool_data.get("result", "")
                    if len(result_text) > max_tool_result_size:
                        result_text = result_text[:max_tool_result_size] + f"\n... [truncated, full length: {len(tool_data.get('result', ''))} chars]"

                    # Add as a system message with tool result context
                    tool_context = f"[Tool: {tool_name}] Query: {tool_data.get('query', tool_data.get('input', ''))}\nResult: {result_text}"
                    results.append({"role": "system", "content": tool_context})

        return results

    def cleanup_inactive(self, max_age_hours: float = 24.0) -> int:
        """Remove sessions inactive longer than threshold. Returns count cleaned."""
        now = time.monotonic()
        max_age = max_age_hours * 3600
        to_remove = [
            cid for cid, session in self._sessions.items()
            if (now - session.last_activity) > max_age
        ]
        for cid in to_remove:
            del self._sessions[cid]
        return len(to_remove)
