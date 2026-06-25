"""Discord bot tools package."""

from .web_search import (
    WEB_SEARCH_TOOL,
    FETCH_PAGE_TOOL,
    WEB_SEARCH_TOOLS,
    execute_web_search,
    execute_fetch_page,
)

__all__ = [
    "WEB_SEARCH_TOOL",
    "FETCH_PAGE_TOOL",
    "WEB_SEARCH_TOOLS",
    "execute_web_search",
    "execute_fetch_page",
]