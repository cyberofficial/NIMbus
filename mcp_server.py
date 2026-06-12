"""MCP Server for NIMbus - Provides web search and page fetch tools via Model Context Protocol."""

import os
import json
import hashlib
import time
from pathlib import Path
from mcp.server.fastmcp import FastMCP
import httpx

from websearch.duckduckgo_html import search_duckduckgo

# MCP Configuration from environment variables
WEB_SEARCH_FETCH_TIMEOUT = float(os.getenv("WEB_SEARCH_FETCH_TIMEOUT", "10.0"))
# Cache TTL in seconds, max 1 hour (3600), default 10 minutes (600), 0 = disabled
MCP_CACHE_TTL = min(int(os.getenv("MCP_CACHE_TTL", "600")), 3600)
# Cache directory: hardcoded to NIMBUS_FETCH_CACHE folder next to mcp_server.py
MCP_CACHE_DIR = str(Path(__file__).parent / "NIMBUS_FETCH_CACHE")

mcp = FastMCP("nimbus", json_response=True)


# Cache utilities
def _get_cache_dir() -> Path:
    """Get or create cache directory (hardcoded to NIMBUS_FETCH_CACHE next to mcp_server.py)."""
    cache_dir = Path(MCP_CACHE_DIR)
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


def _normalize_url(url: str) -> str:
    """Normalize URL for consistent caching."""
    from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
    parsed = urlparse(url)
    # Sort query parameters for consistent caching
    query = parse_qs(parsed.query, keep_blank_values=True)
    sorted_query = urlencode(sorted(query.items()), doseq=True)
    # Strip fragment
    normalized = urlunparse((
        parsed.scheme, parsed.netloc, parsed.path, parsed.params, sorted_query, ""
    ))
    return normalized


def _get_cache_key(url: str) -> str:
    """Generate cache key from normalized URL."""
    normalized = _normalize_url(url)
    return hashlib.sha256(normalized.encode()).hexdigest()[:16]


def _get_cache_paths(cache_key: str) -> tuple[Path, Path]:
    """Get metadata and content cache file paths."""
    cache_dir = _get_cache_dir()
    return (
        cache_dir / f"{cache_key}.json",  # metadata
        cache_dir / f"{cache_key}.txt",   # full content
    )


def _is_cache_valid(meta_path: Path) -> bool:
    """Check if cache entry exists and is not expired."""
    if not meta_path.exists():
        return False
    try:
        with open(meta_path, "r") as f:
            meta = json.load(f)
        # Check if caching is disabled (TTL=0)
        if MCP_CACHE_TTL <= 0:
            return False
        return time.time() < meta.get("expires_at", 0)
    except (json.JSONDecodeError, OSError):
        return False


def _read_cache(cache_key: str) -> tuple[dict, str] | None:
    """Read cached metadata and full content."""
    meta_path, content_path = _get_cache_paths(cache_key)
    if not _is_cache_valid(meta_path):
        return None
    try:
        with open(meta_path, "r") as f:
            meta = json.load(f)
        with open(content_path, "r", encoding="utf-8") as f:
            content = f.read()
        return meta, content
    except OSError:
        return None


def _write_cache(cache_key: str, url: str, content: str) -> dict:
    """Write metadata and full content to cache."""
    meta_path, content_path = _get_cache_paths(cache_key)
    now = time.time()
    expires_at = now + MCP_CACHE_TTL
    meta = {
        "url": url,
        "normalized_url": _normalize_url(url),
        "total_length": len(content),
        "cached_at": now,
        "expires_at": expires_at,
    }
    try:
        with open(meta_path, "w") as f:
            json.dump(meta, f)
        with open(content_path, "w", encoding="utf-8") as f:
            f.write(content)
    except OSError:
        pass  # Cache write failure shouldn't break the request
    return meta


def _extract_text(html: str) -> str:
    """Extract plain text from HTML."""
    from html import unescape
    import re
    text = unescape(html)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


@mcp.tool()
async def web_search(query: str) -> str:
    """Search the web using DuckDuckGo HTML and return formatted results.

    Args:
        query: Search query string
    """
    results = await search_duckduckgo(query)
    if not results:
        return "No results found."
    lines = []
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. {r['title']}")
        lines.append(f"   {r['snippet']}")
        lines.append(f"   {r['url']}")
    return "\n".join(lines)


@mcp.tool()
async def fetch_page(url: str, offset: int = 0, limit: int = 10000, refresh: bool = False) -> str:
    """Fetch and extract text content from a webpage with chunked reading support.

    Uses file-based caching (TTL: MCP_CACHE_TTL seconds, default 600s) to avoid
    re-fetching the same page. Set refresh=True to force a fresh fetch.
    Set MCP_CACHE_TTL=0 to disable caching entirely.

    Returns JSON with content chunk and metadata.

    Args:
        url: URL to fetch
        offset: Character offset to start reading from (default: 0)
        limit: Maximum characters to return (default: 10000)
        refresh: Force fresh fetch, bypassing cache (default: False)
    """
    timeout = float(os.getenv("WEB_SEARCH_FETCH_TIMEOUT", "10.0"))
    cache_key = _get_cache_key(url)

    # Try cache first (unless refresh requested or caching disabled)
    if not refresh and MCP_CACHE_TTL > 0:
        cached = _read_cache(cache_key)
        if cached:
            meta, full_content = cached
            total_length = meta["total_length"]
            start = max(0, min(offset, total_length))
            end = min(start + limit, total_length)
            chunk = full_content[start:end]

            import datetime
            expires_at = datetime.datetime.fromtimestamp(meta["expires_at"], tz=datetime.timezone.utc).isoformat()

            return json.dumps({
                "content": chunk,
                "total_length": total_length,
                "offset": start,
                "limit": limit,
                "cached": True,
                "cache_expires_at": expires_at,
            })

    # Cache miss or refresh - fetch fresh
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(url, timeout=timeout)
        resp.raise_for_status()
        full_content = _extract_text(resp.text)

    # Write to cache (if caching enabled)
    if MCP_CACHE_TTL > 0:
        meta = _write_cache(cache_key, url, full_content)
    else:
        meta = {
            "url": url,
            "normalized_url": _normalize_url(url),
            "total_length": len(full_content),
            "cached_at": time.time(),
            "expires_at": time.time(),
        }
    total_length = meta["total_length"]
    start = max(0, min(offset, total_length))
    end = min(start + limit, total_length)
    chunk = full_content[start:end]

    import datetime
    expires_at = datetime.datetime.fromtimestamp(meta["expires_at"], tz=datetime.timezone.utc).isoformat()

    return json.dumps({
        "content": chunk,
        "total_length": total_length,
        "offset": start,
        "limit": limit,
        "cached": False,
        "cache_expires_at": expires_at,
    })


if __name__ == "__main__":
    mcp.run(transport="stdio")