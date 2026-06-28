"""Native DuckDuckGo HTML search implementation.

Direct HTTP requests to DuckDuckGo's HTML endpoint with lxml parsing.
Supports pagination to fetch more than 10 results.
No external search library dependency (Playwright optional for JS-heavy scenarios).
"""

import os
import urllib.parse
from pathlib import Path

from lxml import html
_HAS_PLAYWRIGHT = None
_HAS_STEALTH = None


def _check_playwright():
    """Lazy check for playwright availability (retries on each call)."""
    global _HAS_PLAYWRIGHT
    if _HAS_PLAYWRIGHT is not None:
        return _HAS_PLAYWRIGHT
    try:
        from playwright.async_api import async_playwright as _pw_async
        _HAS_PLAYWRIGHT = True
    except ImportError:
        _HAS_PLAYWRIGHT = False
    return _HAS_PLAYWRIGHT


def _check_stealth():
    """Lazy check for playwright-stealth availability."""
    global _HAS_STEALTH
    if _HAS_STEALTH is not None:
        return _HAS_STEALTH
    try:
        from playwright_stealth import stealth_async
        _HAS_STEALTH = True
    except ImportError:
        _HAS_STEALTH = False
    return _HAS_STEALTH

from loguru import logger


class DuckDuckGoHTMLSearch:
    """Native DuckDuckGo HTML search using direct HTTP requests."""

    # Try multiple DuckDuckGo endpoints (html first, lite as fallback)
    BASE_URLS = [
        "https://html.duckduckgo.com/html/",
        "https://lite.duckduckgo.com/lite/",
    ]
    USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    RESULTS_PER_PAGE = 10  # DuckDuckGo shows ~10 results per page

    def __init__(self, timeout: float = 10.0) -> None:
        """Initialize the search client.

        Args:
            timeout: HTTP request timeout in seconds.
        """
        self.timeout = timeout
        self._playwright = None
        self._browser = None
        self._context = None
        self._storage_file = Path(__file__).parent.parent / ".playwright_data" / "ddg_state.json"

    async def _get_browser(self):
        """Lazy-init Playwright browser with anti-detection (reused across searches)."""
        if self._browser is None:
            from playwright.async_api import async_playwright
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                headless=os.getenv("DISCORD_BROWSER_HEADLESS", os.getenv("MCP_BROWSER_HEADLESS", "true")).lower() != "false",
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                ],
            )
        return self._browser

    async def _fetch_via_playwright(self, query: str, offset: int = 0) -> str:
        """Fetch DuckDuckGo lite results using Playwright browser with persistent context."""
        browser = await self._get_browser()
        # Load or create persistent context (reuses cookies/state across searches)
        self._storage_file.parent.mkdir(parents=True, exist_ok=True)
        storage_state = None
        if self._storage_file.exists():
            try:
                storage_state = json.loads(self._storage_file.read_text())
            except Exception:
                pass
        if self._context is None:
            self._context = await browser.new_context(
                user_agent=self.USER_AGENT,
                viewport={"width": 1280, "height": 720},
                locale="en-US",
                storage_state=storage_state,
            )
        page = await self._context.new_page()
        # Apply comprehensive stealth patches (only on first page)
        if _check_stealth() and not getattr(self, "_stealth_applied", False):
            from playwright_stealth import stealth_async
            await stealth_async(page)
            self._stealth_applied = True

        # Try html.duckduckgo.com first via Playwright
        try:
            for endpoint, url_fmt in [
                ("html", "https://html.duckduckgo.com/html/?q={}"),
                ("lite", "https://lite.duckduckgo.com/lite/?q={}"),
            ]:
                url = url_fmt.format(urllib.parse.quote(query))
                if offset > 0:
                    url += f"&s={offset}"
                try:
                    logger.info(f"DuckDuckGo Playwright trying {endpoint} endpoint")
                    await page.goto(url, wait_until="networkidle", timeout=15000)
                    html_content = await page.content()
                    has_results = 'class="result"' in html_content or 'class="result-link"' in html_content
                    if has_results:
                        logger.info(f"DuckDuckGo success via Playwright on {endpoint}.duckduckgo.com")
                        return html_content
                    else:
                        logger.warning(f"DuckDuckGo {endpoint} returned page with no result markers ({len(html_content)} chars)")
                except Exception as e:
                    logger.warning(f"DuckDuckGo {endpoint} via Playwright failed: {e}")
            raise RuntimeError("Playwright search failed on all endpoints")
        finally:
            # Save cookies/storage state for next search
            try:
                state = await self._context.storage_state()
                self._storage_file.write_text(json.dumps(state))
            except Exception:
                pass
            # Close the page but keep the context for reuse
            await page.close()

    async def search(
        self, query: str, max_results: int | None = None
    ) -> list[dict]:
        """Search DuckDuckGo and return structured results with pagination support.

        Args:
            query: Search query string.
            max_results: Maximum number of results to return. None = all found.

        Returns:
            List of dicts with keys: title, url, snippet.
        """
        if max_results is not None and max_results <= self.RESULTS_PER_PAGE:
            # Single page request is enough
            html_content = await self._fetch_html(query)
            results = self._parse_results(html_content)
            return results[:max_results] if max_results else results

        # Fetch multiple pages to get more results
        all_results = []
        page = 0
        seen_urls: set[str] = set()

        while max_results is None or len(all_results) < max_results:
            # Calculate offset for pagination (s parameter)
            offset = page * self.RESULTS_PER_PAGE
            try:
                html_content = await self._fetch_html(query, offset=offset)
            except RuntimeError:
                # Pagination page failed (e.g., 202 rate limit) - stop but keep existing results
                logger.warning(f"DuckDuckGo pagination failed at page {page + 1} (offset={offset}) - returning partial results")
                break

            if not html_content:
                break

            results = self._parse_results(html_content)

            # Filter out duplicates
            new_results = []
            for result in results:
                url = result["url"]
                if url not in seen_urls:
                    seen_urls.add(url)
                    new_results.append(result)

            if not new_results:
                # No more results
                break

            all_results.extend(new_results)

            if max_results is not None and len(all_results) >= max_results:
                break

            # Check if we got fewer results than expected (last page)
            if len(results) < self.RESULTS_PER_PAGE:
                break

            page += 1

        return all_results[:max_results] if max_results else all_results

    async def _fetch_html(self, query: str, offset: int = 0) -> str:
        """Fetch DuckDuckGo results using Playwright browser only.

        Args:
            query: Search query string.
            offset: Result offset for pagination (0, 10, 20, ...).
        """
        return await self._fetch_via_playwright(query, offset)

    def _parse_results(self, html_content: str) -> list[dict]:
        """Parse HTML and extract search results.

        Auto-detects format: html.duckduckgo.com (div-based) vs lite.duckduckgo.com (table-based).
        Returns list of dicts with keys: title, url, snippet
        """
        if not html_content:
            return []

        try:
            tree = html.fromstring(html_content)
        except Exception as e:
            logger.warning(f"Failed to parse HTML: {e}")
            return []

        # Auto-detect: lite uses <table> with class="result-link", html uses div.result__body
        if tree.xpath('//a[contains(@class, "result-link")]'):
            return self._parse_lite_results(tree)

        return self._parse_html_results(tree)

    def _parse_html_results(self, tree) -> list[dict]:
        """Parse html.duckduckgo.com div-based results."""
        results = []

        try:
            result_elements = tree.xpath('//div[contains(@class, "result")]')
        except Exception:
            return []

        seen_urls: set[str] = set()
        for element in result_elements:
            try:
                result = self._extract_result(element)
                if result and result.get("title") and result.get("url"):
                    url = result["url"]
                    if url not in seen_urls:
                        seen_urls.add(url)
                        results.append(result)
            except Exception:
                continue

        return results

    def _parse_lite_results(self, tree) -> list[dict]:
        """Parse lite.duckduckgo.com table-based results.

        Lite HTML structure:
          <table>
            <tr><td>1. </td><td><a class="result-link" href="...">Title</a></td></tr>
            <tr><td></td><td class="result-snippet">Snippet text with <b>tags</b></td></tr>
            <tr><td></td><td><span class="link-text">example.com/path</span></td></tr>
            <tr><td></td><td></td></tr>  <!-- spacer -->
        """
        results = []
        seen_urls: set[str] = set()

        # Find all result-link <a> elements
        link_elements = tree.xpath('//a[contains(@class, "result-link")]')
        for link in link_elements:
            try:
                href = link.get("href", "")
                title = link.text_content().strip()

                if not title or not href:
                    continue

                # Clean up URL (remove DuckDuckGo redirect wrapper)
                url = href
                # Prepend scheme if protocol-relative URLs
                if href.startswith("//"):
                    href = f"https:{href}"
                if "uddg=" in href:
                    parsed = urllib.parse.urlparse(href)
                    qs = urllib.parse.parse_qs(parsed.query)
                    if "uddg" in qs:
                        url = urllib.parse.unquote(qs["uddg"][0])
                else:
                    url = href

                if url in seen_urls:
                    continue
                seen_urls.add(url)

                # Find the snippet - it's in the row after the link's row
                # lite structure: <tr> with link, then <tr> with snippet, then <tr> with link-text
                snippet = ""
                parent_tr = link.xpath('ancestor::tr[1]')
                if parent_tr:
                    # Next row after parent_tr
                    following_rows = parent_tr[0].xpath('following-sibling::tr[position()=1]')
                    if following_rows:
                        snippet_cells = following_rows[0].xpath('.//td[contains(@class, "result-snippet")]')
                        if snippet_cells:
                            snippet = snippet_cells[0].text_content().strip()

                results.append({"title": title, "url": url, "snippet": snippet})
            except Exception:
                continue

        return results

    def _extract_result(self, element) -> dict | None:
        """Extract result data from a single result element."""
        try:
            title_links = element.xpath('.//*[contains(@class, "result__title")]//a[@href]')
            if not title_links:
                return None
            link = title_links[0]
            url = link.get("href", "")
            title = link.text_content().strip()

            if not url or not title:
                return None

            # Clean up URL (remove DuckDuckGo redirect)
            if url.startswith("//") or url.startswith("http"):
                parsed = urllib.parse.urlparse(url if url.startswith("http") else f"https:{url}")
                qs = urllib.parse.parse_qs(parsed.query)
                if "uddg" in qs:
                    url = urllib.parse.unquote(qs["uddg"][0])

            # Extract snippet
            snippet = ""
            try:
                snippet_els = element.xpath('.//*[contains(@class, "result__snippet")]')
                if snippet_els:
                    snippet = snippet_els[0].text_content().strip()
            except Exception:
                pass

            return {"title": title, "url": url, "snippet": snippet}
        except Exception:
            return None


# Global instance for reuse
_ddg_instance = None


def get_ddg_instance(timeout: float = 10.0) -> "DuckDuckGoHTMLSearch":
    """Get or create global DuckDuckGoHTMLSearch instance."""
    global _ddg_instance
    if _ddg_instance is None:
        _ddg_instance = DuckDuckGoHTMLSearch()
    return _ddg_instance


async def search_duckduckgo(query: str, max_results: int | None = None) -> list[dict]:
    """Convenience function for searching DuckDuckGo.

    Args:
        query: Search query string.
        max_results: Maximum number of results to return.

    Returns:
        List of dicts with keys: title, url, snippet.
    """
    instance = get_ddg_instance()
    return await instance.search(query, max_results)