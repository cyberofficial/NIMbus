"""Shared effort/budget storage for per-session tracking."""

# Module-level storage for process-wide sharing
_effort_levels: dict[str, str] = {}
_effort_budgets: dict[str, int] = {}


def get_effort_level(session_id: str) -> str | None:
    """Get stored named effort level for a session."""
    return _effort_levels.get(session_id)


def set_effort_level(session_id: str, effort: str) -> None:
    """Store named effort level for a session."""
    _effort_levels[session_id] = effort


def clear_effort_level(session_id: str) -> None:
    """Clear stored named effort level for a session."""
    _effort_levels.pop(session_id, None)


def get_effort_budget(session_id: str) -> int | None:
    """Get stored custom budget override for a session."""
    return _effort_budgets.get(session_id)


def set_effort_budget(session_id: str, budget: int) -> None:
    """Store custom budget override for a session."""
    _effort_budgets[session_id] = budget


def clear_effort_budget(session_id: str) -> None:
    """Clear stored custom budget override for a session."""
    _effort_budgets.pop(session_id, None)


def clear_session(session_id: str) -> None:
    """Clear all effort/budget data for a session."""
    _effort_levels.pop(session_id, None)
    _effort_budgets.pop(session_id, None)