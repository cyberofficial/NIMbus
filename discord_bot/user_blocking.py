"""User blocking management for Discord bot."""

import json
from pathlib import Path
from typing import Optional

from loguru import logger

# Data file for blocked users
BLOCKED_USERS_FILE = Path(".discord_data") / "blocked_users.json"


def _ensure_data_dir():
    """Ensure data directory exists."""
    Path(".discord_data").mkdir(exist_ok=True)


def load_blocked_users() -> set[int]:
    """Load blocked user IDs from file."""
    if not BLOCKED_USERS_FILE.exists():
        return set()

    try:
        with open(BLOCKED_USERS_FILE, 'r') as f:
            data = json.load(f)
        return set(data.get('blocked_users', []))
    except Exception as e:
        logger.error(f"Failed to load blocked users: {e}")
        return set()


def save_blocked_users(blocked_users: set[int]) -> None:
    """Save blocked user IDs to file."""
    _ensure_data_dir()
    try:
        with open(BLOCKED_USERS_FILE, 'w') as f:
            json.dump({'blocked_users': list(blocked_users)}, f, indent=2)
    except Exception as e:
        logger.error(f"Failed to save blocked users: {e}")


def block_user(user_id: int) -> bool:
    """Block a user. Returns True if user was newly blocked, False if already blocked."""
    blocked = load_blocked_users()
    if user_id in blocked:
        return False
    blocked.add(user_id)
    save_blocked_users(blocked)
    logger.info(f"Blocked user {user_id}")
    return True


def unblock_user(user_id: int) -> bool:
    """Unblock a user. Returns True if user was unblocked, False if not blocked."""
    blocked = load_blocked_users()
    if user_id not in blocked:
        return False
    blocked.remove(user_id)
    save_blocked_users(blocked)
    logger.info(f"Unblocked user {user_id}")
    return True


def is_blocked(user_id: int) -> bool:
    """Check if a user is blocked."""
    return user_id in load_blocked_users()


def get_blocked_users() -> set[int]:
    """Get set of all blocked user IDs."""
    return load_blocked_users()
