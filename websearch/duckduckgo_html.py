"""Native DuckDuckGo HTML search implementation.

Direct HTTP requests to DuckDuckGo's HTML endpoint with lxml parsing.
Supports pagination to fetch more than 10 results.
No external search library dependency (Playwright optional for JS-heavy scenarios).
"""

import urllib.parse

import httpx
from lxml import html

from loguru import logger


class DuckDuckGoHTMLSearch:
    """Native DuckDuckGo HTML search using direct HTTP requests."""

    BASE_URL = "https://html.duckduckgo.com/html/"
    USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    RESULTS_PER_PAGE = 10  # DuckDuckGo shows ~10 results per page

    def __init__(self, timeout: float = 10.0) -> None:
        """Initialize the search client.

        Args:
            timeout: HTTP request timeout in seconds.
        """
        self.timeout = timeout

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
            html_content = await self._fetch_html(query, offset=offset)

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
        """Fetch HTML from DuckDuckGo's HTML endpoint with optional pagination offset.

        Args:
            query: Search query string.
            offset: Result offset for pagination (0, 10, 20, ...).
        """
        headers = {
            "User-Agent": self.USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }

        params = {"q": query}
        if offset > 0:
            params["s"] = str(offset)  # DuckDuckGo pagination parameter

        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
            try:
                response = await client.get(
                    self.BASE_URL,
                    params=params,
                    headers=headers,
                    follow_redirects=True,
                )
            except Exception as e:
                logger.warning(f"DuckDuckGo HTML request failed: {e}")
                return ""

        if response.status_code != 200:
            logger.warning(f"DuckDuckGo HTML request failed: {response.status_code}")
            return ""

        return response.text

    def _parse_results(self, html_content: str) -> list[dict]:
        """Parse HTML and extract search results.

        Returns list of dicts with keys: title, url, snippet
        """
        if not html_content:
            return []

        try:
            tree = html.fromstring(html_content)
        except Exception as e:
            logger.warning(f"Failed to parse HTML: {e}")
            return []

        results = []

        # DuckDuckGo HTML structure: results are in .result or .result__body containers
        # Each result has: title in .result__title > a, snippet in .result__snippet
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