"""MCP Server for NIMbus - Provides web search and page fetch tools via Model Context Protocol."""

import os
from mcp.server.fastmcp import FastMCP
import httpx

from websearch.duckduckgo_html import search_duckduckgo

# MCP Configuration from environment variables
WEB_SEARCH_FETCH_TIMEOUT = float(os.getenv("WEB_SEARCH_FETCH_TIMEOUT", "10.0"))

mcp = FastMCP("nimbus", json_response=True)


@mcp.tool()
async def web_search(query: str, max_results: int | None = None) -> str:
    """Search the web using DuckDuckGo HTML and return formatted results.

    Args:
        query: Search query string
        max_results: Maximum number of results to return. None = return all found results.
    """
    results = await search_duckduckgo(query, max_results)
    if not results:
        return "No results found."
    lines = []
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. {r['title']}")
        lines.append(f"   {r['snippet']}")
        lines.append(f"   {r['url']}")
    return "\n".join(lines)


@mcp.tool()
async def fetch_page(url: str, max_chars: int = 10000) -> str:
    """Fetch and extract text content from a webpage.

    Uses the system-configured fetch timeout from WEB_SEARCH_FETCH_TIMEOUT env var.
    """
    timeout = float(os.getenv("WEB_SEARCH_FETCH_TIMEOUT", "10.0"))
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(url, timeout=timeout)
        resp.raise_for_status()
        # Simple extraction - could use BeautifulSoup or playwright later
        from html import unescape
        text = unescape(resp.text)

        # Basic HTML tag removal
        import re
        text = re.sub(r'<[^>]+>', ' ', text)
        text = re.sub(r'\s+', ' ', text)
        return text[:10000]


if __name__ == "__main__":
    mcp.run(transport="stdio")