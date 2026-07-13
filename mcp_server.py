"""MCP Server for NIMbus - Provides web search and page fetch tools via Model Context Protocol."""

import os
import json
import hashlib
import time
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from mcp.server.fastmcp import FastMCP
import httpx

from websearch.duckduckgo_html import search_duckduckgo

# MCP Configuration from environment variables
WEB_SEARCH_FETCH_TIMEOUT = float(os.getenv("WEB_SEARCH_FETCH_TIMEOUT", "10.0"))
# Cache TTL in seconds, max 1 hour (3600), default 10 minutes (600), 0 = disabled
MCP_CACHE_TTL = min(int(os.getenv("MCP_CACHE_TTL", "600")), 3600)
# Cache directory: hardcoded to NIMBUS_FETCH_CACHE folder next to mcp_server.py
MCP_CACHE_DIR = str(Path(__file__).parent / "NIMBUS_FETCH_CACHE")
# Browser settings: MCP_BROWSER_HEADLESS=true means use fast HTTP; false means always use browser
MCP_BROWSER_HEADLESS = os.getenv("MCP_BROWSER_HEADLESS", "true").lower() == "true"
DEFAULT_USE_BROWSER = not MCP_BROWSER_HEADLESS  # Use browser by default if not headless

mcp = FastMCP("nimbus", json_response=True)


# ============================================================================
# Cache Utilities
# ============================================================================

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
    text = unescape(html)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


async def _fetch_via_playwright(url: str, timeout: float = 30.0) -> str:
    """Fetch page using Playwright browser (JS rendering, handles SPAs/anti-bot)."""
    from websearch.duckduckgo_html import get_ddg_instance

    ddg = get_ddg_instance()
    browser = await ddg._get_browser()

    # Reuse persistent context (cookies, stealth)
    if ddg._context is None:
        ddg._context = await browser.new_context(
            user_agent=ddg.USER_AGENT,
            viewport={"width": 1280, "height": 720},
            locale="en-US",
        )

    page = await ddg._context.new_page()
    try:
        await page.goto(url, wait_until="networkidle", timeout=int(timeout * 1000))
        html_content = await page.content()
        return _extract_text(html_content)
    finally:
        await page.close()


# ============================================================================
# Search Utilities (New)
# ============================================================================

@dataclass
class SearchMatch:
    """A single search match within a cached file."""
    cache_key: str
    url: str
    line_number: int
    char_position: int  # Position of the match start in the full file
    matched_text: str   # The actual matched line/segment
    before_context: str = ""
    after_context: str = ""


@dataclass
class SearchResult:
    """Aggregated search results across cache."""
    query: str
    total_matches: int
    files_searched: int
    matches: list[SearchMatch]


def _list_cache_entries() -> list[tuple[str, dict]]:
    """List all valid cache entries with metadata.

    Returns list of (cache_key, metadata_dict) tuples.
    """
    cache_dir = _get_cache_dir()
    entries = []
    for meta_file in cache_dir.glob("*.json"):
        cache_key = meta_file.stem
        content_file = cache_dir / f"{cache_key}.txt"
        if not content_file.exists():
            continue
        try:
            with open(meta_file, "r") as f:
                meta = json.load(f)
            if _is_cache_valid(meta_file):
                entries.append((cache_key, meta))
        except (json.JSONDecodeError, OSError):
            continue
    return entries


def _read_full_cache_content(cache_key: str) -> str | None:
    """Read full cached content for a cache key."""
    _, content_path = _get_cache_paths(cache_key)
    if not content_path.exists():
        return None
    try:
        with open(content_path, "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


def _build_line_index(content: str) -> list[tuple[int, int]]:
    """Build index of (line_start_char, line_end_char) for each line.

    Returns list where each element is (start_char_pos, end_char_pos)
    for that line. end_char_pos is exclusive (position after newline).
    """
    line_starts = [0]
    line_ends = []
    for i, ch in enumerate(content):
        if ch == '\n':
            line_ends.append(i + 1)  # position after newline
            line_starts.append(i + 1)
    # Handle last line (no trailing newline)
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
    return len(line_index)  # last line if beyond


def _get_line_content(line_index: list[tuple[int, int]], content: str, line_num: int) -> str:
    """Get full line content by 1-based line number."""
    if 1 <= line_num <= len(line_index):
        start, end = line_index[line_num - 1]
        return content[start:end]
    return ""


def _expand_to_line_boundaries(line_index: list[tuple[int, int]], content: str,
                                start: int, end: int) -> tuple[int, int]:
    """Expand start/end to include complete lines.

    If start/end fall in middle of line, expand to line boundaries.
    """
    start_line = _find_line_for_char(line_index, start)
    end_line = _find_line_for_char(line_index, max(end - 1, 0))

    start = line_index[start_line - 1][0]
    end = line_index[end_line - 1][1]

    return start, end


def _search_in_content(content: str, query: str, case_sensitive: bool = False) -> list[tuple[int, int]]:
    """Find all matches of query in content.

    Returns list of (start_char, end_char) tuples.
    """
    flags = 0 if case_sensitive else re.IGNORECASE
    matches = []
    for match in re.finditer(re.escape(query), content, flags):
        matches.append((match.start(), match.end()))
    return matches


def _extract_match_with_context(content: str, line_index: list[tuple[int, int]],
                                 match_start: int, match_end: int,
                                 before_chars: int = 400, after_chars: int = 500) -> dict:
    """Extract matched line plus surrounding context, expanded to line boundaries.

    Returns dict with:
    - line_number: 1-based line number of match
    - char_position: match start position in file
    - matched_line: the full line containing the match
    - context_before: text before match (expanded to line start)
    - context_after: text after match (expanded to line end)
    - full_snippet: combined context suitable for display
    """
    line_num = _find_line_for_char(line_index, match_start)
    line_start, line_end = line_index[line_num - 1]

    # Calculate context bounds
    context_start = max(0, match_start - before_chars)
    context_end = min(len(content), match_end + after_chars)

    # Expand to line boundaries
    context_start, context_end = _expand_to_line_boundaries(
        line_index, content, context_start, context_end
    )

    # Extract pieces
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


def _search_cache_files(query: str, case_sensitive: bool = False,
                         before_chars: int = 400, after_chars: int = 500,
                         max_results: int = 50) -> SearchResult:
    """Search all cached files for query.

    Args:
        query: Search term
        case_sensitive: Whether to match case
        before_chars: Characters to include before match
        after_chars: Characters to include after match
        max_results: Maximum total matches to return

    Returns SearchResult with matches across all cached files.
    """
    entries = _list_cache_entries()
    all_matches = []
    files_searched = 0

    for cache_key, meta in entries:
        content = _read_full_cache_content(cache_key)
        if not content:
            continue
        files_searched += 1

        line_index = _build_line_index(content)
        matches = _search_in_content(content, query, case_sensitive)

        for match_start, match_end in matches:
            if len(all_matches) >= max_results:
                break

            context = _extract_match_with_context(
                content, line_index, match_start, match_end,
                before_chars, after_chars
            )

            all_matches.append(SearchMatch(
                cache_key=cache_key,
                url=meta.get("url", "unknown"),
                line_number=context["line_number"],
                char_position=context["char_position"],
                matched_text=context["matched_line"],
                before_context=context["context_before"],
                after_context=context["context_after"],
            ))
        if len(all_matches) >= max_results:
            break

    return SearchResult(
        query=query,
        total_matches=len(all_matches),
        files_searched=files_searched,
        matches=all_matches,
    )


# ============================================================================
# MCP Tools
# ============================================================================

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
async def fetch_page(url: str, offset: int = 0, limit: int = 10000,
                      refresh: bool = False, search: Optional[str] = None,
                      use_browser: bool = DEFAULT_USE_BROWSER) -> str:
    """Fetch and extract text content from a webpage with chunked reading support.

    Uses file-based caching (TTL: MCP_CACHE_TTL seconds, default 600s) to avoid
    re-fetching the same page. Set refresh=True to force a fresh fetch.
    Set MCP_CACHE_TTL=0 to disable caching entirely.

    If `search` is provided, returns matches for that term within the page
    with line numbers and character positions instead of a chunk.

    When MCP_BROWSER_HEADLESS=false, a visible browser is used for all fetches.
    When MCP_BROWSER_HEADLESS=true, fast HTTP (httpx) is used by default;
    set use_browser=true to enable Playwright for JS-rendered content.

    Args:
        url: URL to fetch
        offset: Character offset to start reading from (for chunked reading)
        limit: Max characters to return (use with offset for chunks)
        search: Optional search term (capped at 5 matches, 200 chars context each)
        refresh: Force fresh fetch, bypassing cache (default: False)
        use_browser: Use Playwright browser for JS-rendered content. Ignored when
                     MCP_BROWSER_HEADLESS=false (always uses browser).
    """
    import datetime
    from loguru import logger
    timeout = float(os.getenv("WEB_SEARCH_FETCH_TIMEOUT", "10.0"))
    cache_key = _get_cache_key(url)

    # Force browser when headless is disabled - no override allowed
    effective_use_browser = True if not MCP_BROWSER_HEADLESS else use_browser

    # Try cache first (unless refresh requested or caching disabled)
    cached = False
    full_content = None
    meta = None

    if not refresh and MCP_CACHE_TTL > 0:
        cached_data = _read_cache(cache_key)
        if cached_data:
            cached = True
            meta, full_content = cached_data

    if full_content is None:
        # Cache miss or refresh - fetch fresh
        logger.info(f"[MCP FETCH] url={url} | cache=MISS | fetching... (browser={effective_use_browser})")
        if effective_use_browser:
            full_content = await _fetch_via_playwright(url, timeout=30.0)
        else:
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

    if meta is None:
        meta = {}
    total_length = meta.get("total_length", 0)
    expires_at = datetime.datetime.fromtimestamp(meta.get("expires_at", 0), tz=datetime.timezone.utc).isoformat()

    # NEW: If search provided, return search results instead of chunk
    if search is not None:
        line_index = _build_line_index(full_content)
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

        match_list = []
        for match_start, match_end in matches:
            context = _extract_match_with_context(
                full_content, line_index, match_start, match_end,
                before_chars=400, after_chars=500
            )
            match_list.append({
                "line_number": context["line_number"],
                "char_position": context["char_position"],
                "matched_line": context["matched_line"],
                "context_before": context["context_before"],
                "context_after": context["context_after"],
                "full_snippet": context["full_snippet"],
            })

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

    return json.dumps({
        "content": chunk,
        "total_length": total_length,
        "offset": start,
        "limit": limit,
        "cached": cached,
        "cache_expires_at": expires_at,
    })


@mcp.tool()
async def search_cache(query: str, case_sensitive: bool = False,
                        max_results: int = 50) -> str:
    """Search all cached pages for a keyword/phrase.

    Returns matching lines with line numbers, character positions, and file URLs.
    Useful for finding specific terms across all previously fetched documentation.

    Args:
        query: Search term (exact phrase match)
        case_sensitive: Match case exactly (default: false)
        max_results: Maximum matches to return (default: 50)
    """
    result = _search_cache_files(query, case_sensitive,
                                  before_chars=0, after_chars=0,
                                  max_results=max_results)

    if not result.matches:
        return f"No matches found for '{query}' in {result.files_searched} cached file(s)."

    lines = [f"Found {result.total_matches} match(es) across {result.files_searched} cached file(s):"]
    for match in result.matches:
        lines.append(f"\n-----")
        lines.append(f"<{match.url}> - Line: {match.line_number} - Char Position: {match.char_position:,}")
        lines.append(f"```")
        lines.append(match.matched_text.rstrip())
        lines.append(f"```")
        lines.append(f"-----")

    return "\n".join(lines)


@mcp.tool()
async def search_cache_snippet(query: str, before_chars: int = 400, after_chars: int = 500,
                                case_sensitive: bool = False, max_results: int = 20) -> str:
    """Search cached pages and return code snippets with surrounding context.

    Expands matches to include complete lines (smart line boundary detection).
    Useful for finding code examples or error messages in documentation.

    Args:
        query: Search term (exact phrase match)
        before_chars: Characters to show before match (expanded to line start)
        after_chars: Characters to show after match (expanded to line end)
        case_sensitive: Match case exactly (default: false)
        max_results: Maximum snippets to return (default: 20)
    """
    result = _search_cache_files(query, case_sensitive,
                                  before_chars=before_chars, after_chars=after_chars,
                                  max_results=max_results)

    if not result.matches:
        return f"No matches found for '{query}' in {result.files_searched} cached file(s)."

    lines = [f"Found {result.total_matches} match(es) across {result.files_searched} cached file(s):"]
    for match in result.matches:
        lines.append(f"\n-----")
        lines.append(f"<{match.url}> - Line: {match.line_number} - Char Position: {match.char_position:,}")
        lines.append(f"```")
        lines.append(match.before_context + match.matched_text + match.after_context)
        lines.append(f"```")
        lines.append(f"-----")

    return "\n".join(lines)


if __name__ == "__main__":
    mcp.run(transport="stdio")