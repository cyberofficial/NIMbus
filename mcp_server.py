"""MCP Server for NIMbus - Provides web search and page fetch tools via Model Context Protocol."""

import os
from mcp.server.fastmcp import FastMCP
from duckduckgo_search import DDGS
import httpx

# MCP Configuration from environment variables
WEB_SEARCH_MAX_RESULTS = int(os.getenv("WEB_SEARCH_MAX_RESULTS", "5"))
WEB_SEARCH_FETCH_TIMEOUT = float(os.getenv("WEB_SEARCH_FETCH_TIMEOUT", "10.0"))
WEB_SEARCH_MAX_CHARS = int(os.getenv("WEB_SEARCH_MAX_CHARS", "10000"))

mcp = FastMCP("nimbus", json_response=True)


@mcp.tool()
def web_search(query: str, max_results: int = WEB_SEARCH_MAX_RESULTS) -> str:
    """Search the web using DuckDuckGo and return formatted results."""
    results = DDGS().text(query, max_results=max_results)
    return "\n".join(
        f"{r['title']}: {r['body']} ({r['href']})" for r in results
    )


@mcp.tool()
def fetch_page(url: str, max_chars: int = WEB_SEARCH_MAX_CHARS) -> str:
    """Fetch and extract text content from a webpage."""
    resp = httpx.get(url, timeout=WEB_SEARCH_FETCH_TIMEOUT)
    resp.raise_for_status()
    # Simple extraction - could use BeautifulSoup or playwright later
    from html import unescape
    text = unescape(resp.text)
    # Basic HTML tag removal
    import re
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'\s+', ' ', text)
    return text[:max_chars]


if __name__ == "__main__":
    mcp.run(transport="stdio")