"""Web search tools for Discord bot.

Provides web_search and fetch_page tools that the model can use to search
the web and fetch page content. Reuses the existing DuckDuckGo implementation.
"""

import json
import os
import re
import time
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

from loguru import logger
import httpx

from api.models.anthropic import Tool
from websearch.duckduckgo_html import search_duckduckgo


# ============================================================================
# Configuration (reuses MCP server env vars)
# ============================================================================

WEB_SEARCH_FETCH_TIMEOUT = float(os.getenv("WEB_SEARCH_FETCH_TIMEOUT", "10.0"))
MCP_CACHE_TTL = min(int(os.getenv("MCP_CACHE_TTL", "600")), 3600)
MCP_CACHE_DIR = str(Path(__file__).parent.parent.parent / "NIMBUS_FETCH_CACHE")


# ============================================================================
# Tool Definitions
# ============================================================================

WEB_SEARCH_TOOL = Tool(
    name="web_search",
    description=(
        "Search the web using DuckDuckGo. Use when user asks for current information, "
        "facts, or things not in training data. IMPORTANT: Do not settle for the first "
        "few results. If you're not confident in the accuracy, refine your query and "
        "search again. Cross-reference multiple sources when possible. Prefer authoritative "
        "sources (official docs, reputable news, academic). Return the refined query you "
        "used so the user knows what was searched."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query string"}
        },
        "required": ["query"]
    }
)

FETCH_PAGE_TOOL = Tool(
    name="fetch_page",
    description=(
        "Fetch and extract text content from a webpage. Use after web_search to get full "
        "content of important results. For large pages (>10000 chars), use offset/limit to "
        "read in chunks (e.g., offset=0 limit=5000, then offset=5000 limit=5000). "
        "Use the 'search' parameter to find specific information within long pages. "
        "Verify claims by fetching primary sources."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "URL to fetch"},
            "offset": {"type": "integer", "default": 0, "description": "Character offset to start reading from (for chunked reading)"},
            "limit": {"type": "integer", "default": 10000, "description": "Max characters to return (use with offset for chunks)"},
            "search": {"type": "string", "description": "Optional search term (capped at 5 matches, 200 chars context each)"}
        },
        "required": ["url"]
    }
)

WEB_SEARCH_TOOLS = [WEB_SEARCH_TOOL, FETCH_PAGE_TOOL]


# ============================================================================
# Cache Utilities (from MCP server)
# ============================================================================

def _get_cache_dir() -> Path:
    """Get or create cache directory."""
    cache_dir = Path(MCP_CACHE_DIR)
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


def _normalize_url(url: str) -> str:
    """Normalize URL for consistent caching."""
    parsed = urlparse(url)
    query = parse_qs(parsed.query, keep_blank_values=True)
    sorted_query = urlencode(sorted(query.items()), doseq=True)
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
        cache_dir / f"{cache_key}.json",
        cache_dir / f"{cache_key}.txt",
    )


def _is_cache_valid(meta_path: Path) -> bool:
    """Check if cache entry exists and is not expired."""
    if not meta_path.exists():
        return False
    try:
        with open(meta_path, "r") as f:
            meta = json.load(f)
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
        pass
    return meta


def _extract_text(html: str) -> str:
    """Extract plain text from HTML."""
    from html import unescape
    text = unescape(html)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


# ============================================================================
# Tool Execution
# ============================================================================

async def execute_web_search(query: str, max_results: int = 5) -> str:
    """Execute web search and return formatted results.

    Args:
        query: Search query string
        max_results: Maximum number of results to return

    Returns:
        Formatted string with search results (title, snippet, URL)
    """
    logger.info(f"[WEB SEARCH] query={query!r} max_results={max_results}")
    results = await search_duckduckgo(query)
    if not results:
        logger.info(f"[WEB SEARCH] query={query!r} | results=0")
        return "No results found."

    results = results[:max_results]
    logger.info(f"[WEB SEARCH] query={query!r} | results={len(results)} | urls={[r['url'] for r in results]}")

    lines = []
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. {r['title']}")
        lines.append(f"   {r['snippet']}")
        lines.append(f"   {r['url']}")
    return "\n".join(lines)


async def execute_fetch_page(
    url: str,
    offset: int = 0,
    limit: int = 10000,
    search: Optional[str] = None,
    refresh: bool = False,
) -> str:
    """Fetch and extract text content from a webpage with caching.

    Args:
        url: URL to fetch
        offset: Character offset to start reading from
        limit: Maximum characters to return
        search: Optional search term to find within page content
        refresh: Force fresh fetch, bypassing cache

    Returns:
        JSON string with content chunk and metadata (or search matches)
    """
    import datetime

    timeout = WEB_SEARCH_FETCH_TIMEOUT
    cache_key = _get_cache_key(url)

    logger.info(f"[WEB FETCH] url={url} offset={offset} limit={limit} search={search!r} refresh={refresh}")

    cached = False
    full_content = None
    meta = None

    if not refresh and MCP_CACHE_TTL > 0:
        cached_data = _read_cache(cache_key)
        if cached_data:
            cached = True
            meta, full_content = cached_data
            logger.info(f"[WEB FETCH] url={url} | cache=HIT | length={len(full_content)}")

    if full_content is None:
        logger.info(f"[WEB FETCH] url={url} | cache=MISS | fetching...")
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(url, timeout=timeout)
            resp.raise_for_status()
            full_content = _extract_text(resp.text)

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
        logger.info(f"[WEB FETCH] url={url} | fetched | length={len(full_content)}")

    if meta is None:
        meta = {}
    total_length = meta.get("total_length", 0)
    expires_at = datetime.datetime.fromtimestamp(meta.get("expires_at", 0), tz=datetime.timezone.utc).isoformat()

    # If search provided, return search matches instead of chunk
    if search is not None:
        matches = _search_in_content(full_content, search, case_sensitive=False)

        if not matches:
            return json.dumps({
                "content": "",
                "total_length": total_length,
                "offset": 0,
                "limit": 0,
                "cached": cached,
                "cache_expires_at": expires_at,
                "search_query": search,
                "matches": [],
            })

        # Limit matches to avoid massive payloads (>26MB NVIDIA limit)
        MAX_MATCHES = 5
        MAX_CONTEXT = 200
        line_index = _build_line_index(full_content)
        match_list = []
        for match_start, match_end in matches[:MAX_MATCHES]:
            context = _extract_match_with_context(
                full_content, line_index, match_start, match_end,
                before_chars=MAX_CONTEXT, after_chars=MAX_CONTEXT
            )
            match_list.append({
                "line_number": context["line_number"],
                "char_position": context["char_position"],
                "matched_line": context["matched_line"],
                "context_before": context["context_before"],
                "context_after": context["context_after"],
                "full_snippet": context["full_snippet"],
            })

        logger.info(f"[WEB FETCH] url={url} | search={search!r} | matches={len(match_list)}")
        return json.dumps({
            "content": "",
            "total_length": total_length,
            "offset": 0,
            "limit": 0,
            "cached": cached,
            "cache_expires_at": expires_at,
            "search_query": search,
            "matches": match_list,
        })

    # Normal chunked reading
    start = max(0, min(offset, total_length))
    end = min(start + limit, total_length)
    chunk = full_content[start:end]

    logger.info(f"[WEB FETCH] url={url} | chunk={start}-{end}/{total_length}")
    return json.dumps({
        "content": chunk,
        "total_length": total_length,
        "offset": start,
        "limit": limit,
        "cached": cached,
        "cache_expires_at": expires_at,
    })


# ============================================================================
# Search Helpers (from MCP server)
# ============================================================================

@dataclass
class SearchMatch:
    cache_key: str
    url: str
    line_number: int
    char_position: int
    matched_text: str
    before_context: str = ""
    after_context: str = ""


def _build_line_index(content: str) -> list[tuple[int, int]]:
    """Build index of (line_start_char, line_end_char) for each line."""
    line_starts = [0]
    line_ends = []
    for i, ch in enumerate(content):
        if ch == '\n':
            line_ends.append(i + 1)
            line_starts.append(i + 1)
    if line_ends and line_ends[-1] > line_starts[-1]:
        line_ends.append(len(content))
    elif not line_ends:
        line_ends.append(len(content))
    return list(zip(line_starts, line_ends))


def _find_line_for_char(line_index: list[tuple[int, int]], char_pos: int) -> int:
    """Find line number (1-based) containing char_pos."""
    for i, (start, end) in enumerate(line_index):
        if start <= char_pos < end:
            return i + 1
    return len(line_index)


def _get_line_content(line_index: list[tuple[int, int]], content: str, line_num: int) -> str:
    """Get full line content by 1-based line number."""
    if 1 <= line_num <= len(line_index):
        start, end = line_index[line_num - 1]
        return content[start:end]
    return ""


def _expand_to_line_boundaries(line_index: list[tuple[int, int]], content: str,
                                start: int, end: int) -> tuple[int, int]:
    """Expand start/end to include complete lines."""
    start_line = _find_line_for_char(line_index, start)
    end_line = _find_line_for_char(line_index, max(end - 1, 0))
    start = line_index[start_line - 1][0]
    end = line_index[end_line - 1][1]
    return start, end


def _search_in_content(content: str, query: str, case_sensitive: bool = False) -> list[tuple[int, int]]:
    """Find all matches of query in content. Returns list of (start_char, end_char) tuples."""
    flags = 0 if case_sensitive else re.IGNORECASE
    matches = []
    for match in re.finditer(re.escape(query), content, flags):
        matches.append((match.start(), match.end()))
    return matches


def _extract_match_with_context(content: str, line_index: list[tuple[int, int]],
                                 match_start: int, match_end: int,
                                 before_chars: int = 400, after_chars: int = 500) -> dict:
    """Extract matched line plus surrounding context, expanded to line boundaries."""
    line_num = _find_line_for_char(line_index, match_start)
    line_start, line_end = line_index[line_num - 1]

    context_start = max(0, match_start - before_chars)
    context_end = min(len(content), match_end + after_chars)
    context_start, context_end = _expand_to_line_boundaries(line_index, content, context_start, context_end)

    matched_line = content[line_start:line_end]
    context_before = content[context_start:match_start]
    context_after = content[match_end:context_end]
    full_snippet = content[context_start:context_end]

    return {
        "line_number": line_num,
        "char_position": match_start,
        "matched_line": matched_line,
        "context_before": context_before,
        "context_after": context_after,
        "full_snippet": full_snippet,
    }