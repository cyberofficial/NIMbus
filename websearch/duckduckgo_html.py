"""Native DuckDuckGo HTML search implementation.

Direct HTTP requests to DuckDuckGo's HTML endpoint with lxml parsing.
No external search library dependency.
"""

import urllib.parse

import httpx
from lxml import html

from loguru import logger


class DuckDuckGoHTMLSearch:
    """Native DuckDuckGo HTML search using direct HTTP requests."""

    BASE_URL = "https://html.duckduckgo.com/html/"
    USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

    def __init__(self, timeout: float = 10.0) -> None:
        """Initialize the search client.

        Args:
            timeout: HTTP request timeout in seconds.
        """
        self.timeout = timeout

    async def search(
        self, query: str, max_results: int | None = None
    ) -> list[dict]:
        """Search DuckDuckGo and return structured results.

        Args:
            query: Search query string.
            max_results: Maximum number of results to return. None = all found.

        Returns:
            List of dicts with keys: title, url, snippet.
        """
        # 1. Fetch HTML from DuckDuckGo
        html_content = await self._fetch_html(query)

        # 2. Parse HTML and extract results
        results = self._parse_results(html_content)

        # 3. Apply max_results limit if provided
        if max_results is not None:
            results = results[:max_results]

        return results

    async def _fetch_html(self, query: str) -> str:
        """Fetch HTML from DuckDuckGo's HTML endpoint."""
        encoded_query = urllib.parse.quote_plus(query)
        url = f"{self.BASE_URL}?q={encoded_query}"

        headers = {
            "User-Agent": self.USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }

        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
            try:
                response = await client.get(
                    self.BASE_URL,
                    params={"q": query},
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