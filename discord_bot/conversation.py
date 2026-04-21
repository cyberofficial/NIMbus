"""Conversation management with token tracking for Discord bot."""

import asyncio
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import tiktoken


@dataclass
class ConversationMessage:
    """A single message with user context."""
    role: str
    content: str
    user_id: Optional[int] = None
    username: str = ""  # Display name for context


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


class ConversationManager:
    """
    Manage conversations per Discord channel with token-based compaction.

    When conversation exceeds threshold (e.g., 80% of max tokens),
    trigger auto-compaction to summarize and reset.
    """

    def __init__(self, max_tokens: int, compact_threshold: float = 0.8, system_prompt: str = ""):
        self._max_tokens = max_tokens
        self._compact_threshold = compact_threshold
        self._encoder = tiktoken.get_encoding("cl100k_base")
        self._system_prompt = system_prompt
        self._system_prompt_tokens = self._count_tokens(system_prompt) if system_prompt else 0

        # Load persisted sessions
        from .persistence import load_conversations
        loaded = load_conversations()
        self._sessions: Dict[int, ConversationSession] = loaded if loaded else {}

    def _count_tokens(self, text: str) -> int:
        """Count tokens using cl100k_base encoding."""
        return len(self._encoder.encode(text))

    def get_session(self, channel_id: int) -> Optional[ConversationSession]:
        """Get or create a conversation session."""
        if channel_id not in self._sessions:
            self._sessions[channel_id] = ConversationSession(channel_id=channel_id)
        return self._sessions.get(channel_id)

    def get_history(self, channel_id: int) -> List[dict]:
        """Get conversation history formatted for NIM API."""
        session = self.get_session(channel_id)
        if not session:
            return []
        return [{"role": m.role, "content": m.content} for m in session.messages]

    def get_history_for_nim(self, channel_id: int) -> List[dict]:
        """Get conversation history with user context for NIM API."""
        session = self.get_session(channel_id)
        if not session:
            return []
        return [
            {
                "role": m.role,
                "content": f"{m.username}: {m.content}" if m.username else m.content
            }
            for m in session.messages
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

    def add_message(self, channel_id: int, role: str, content: str) -> dict:
        """
        Add message and return status.

        Returns: {"status": "ok" | "auto_compact" | "needs_compaction"}
        """
        return self.add_message_with_user(channel_id, role, content, None, "")

    def add_message_with_user(
        self, channel_id: int, role: str, content: str,
        user_id: Optional[int] = None, username: str = ""
    ) -> dict:
        """
        Add message with user context and return status.

        Returns: {"status": "ok" | "auto_compact" | "needs_compaction"}
        """
        msg_tokens = self._count_tokens(content)
        current_tokens = self.get_token_count(channel_id)

        # Check if this message would exceed hard limit
        if current_tokens + msg_tokens > self._max_tokens:
            return {"status": "needs_compaction", "tokens": msg_tokens}

        # Get or create session
        session = self.get_session(channel_id)
        if session is None:
            session = ConversationSession(channel_id=channel_id)
            self._sessions[channel_id] = session

        msg = ConversationMessage(
            role=role, content=content, user_id=user_id, username=username
        )
        session.messages.append(msg)
        session.token_count += msg_tokens
        session.last_activity = time.monotonic()

        # Check if we should auto-compact
        if self.should_compact(channel_id):
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
        """Clear conversation session (for /new command)."""
        if channel_id in self._sessions:
            del self._sessions[channel_id]
        # Persist the clear
        from .persistence import save_conversations
        save_conversations(self._sessions)

    def compact(self, channel_id: int, summary: str) -> None:
        """
        Replace conversation with summary.
        Called after compaction is complete.
        """
        summary_tokens = self._count_tokens(summary)
        self._sessions[channel_id] = ConversationSession(
            channel_id=channel_id,
            messages=[ConversationMessage(role="assistant", content=summary, username="Summary")],
            token_count=summary_tokens,
        )

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
