"""Persistence for Discord conversation history and rate limit state."""

import json
import os
from pathlib import Path
from typing import Optional

from loguru import logger


# Data directory for bot state
DATA_DIR = Path(".discord_data")
CONVERSATIONS_FILE = DATA_DIR / "conversations.json"


def _ensure_data_dir():
    """Ensure data directory exists."""
    DATA_DIR.mkdir(exist_ok=True)


def save_conversations(conversations: dict) -> None:
    """Save conversation sessions to disk."""
    _ensure_data_dir()

    # Convert to serializable format
    data = {}
    for channel_id, session in conversations.items():
        messages = []
        for msg in session.messages:
            messages.append({
                "role": msg.role,
                "content": msg.content,
                "user_id": msg.user_id,
                "username": msg.username,
            })
        data[str(channel_id)] = {
            "messages": messages,
            "token_count": session.token_count,
            "created_at": session.created_at,
            "last_activity": session.last_activity,
        }

    try:
        with open(CONVERSATIONS_FILE, 'w') as f:
            json.dump(data, f, indent=2)
        logger.debug(f"Saved {len(data)} conversation sessions")
    except Exception as e:
        logger.error(f"Failed to save conversations: {e}")


def load_conversations() -> Optional[dict]:
    """Load conversation sessions from disk."""
    if not CONVERSATIONS_FILE.exists():
        return None

    try:
        with open(CONVERSATIONS_FILE, 'r') as f:
            data = json.load(f)

        from .conversation import ConversationSession, ConversationMessage

        sessions = {}
        for channel_id_str, session_data in data.items():
            channel_id = int(channel_id_str)
            session = ConversationSession(
                channel_id=channel_id,
                messages=[],
                token_count=session_data.get("token_count", 0),
                created_at=session_data.get("created_at", 0),
                last_activity=session_data.get("last_activity", 0),
            )
            for msg_data in session_data.get("messages", []):
                msg = ConversationMessage(
                    role=msg_data["role"],
                    content=msg_data["content"],
                    user_id=msg_data.get("user_id"),
                    username=msg_data.get("username", ""),
                )
                session.messages.append(msg)
            sessions[channel_id] = session

        logger.info(f"Loaded {len(sessions)} conversation sessions from disk")
        return sessions
    except Exception as e:
        logger.error(f"Failed to load conversations: {e}")
        return None


def delete_conversations_file() -> None:
    """Delete the conversations file (e.g., on clean shutdown)."""
    if CONVERSATIONS_FILE.exists():
        try:
            CONVERSATIONS_FILE.unlink()
        except Exception as e:
            logger.warning(f"Failed to delete conversations file: {e}")
