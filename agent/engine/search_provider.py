"""
Search Provider — Phase 5: Universal Search API.

Multi-provider search with graceful fallback:
  1. SerpAPI (if SERPAPI_KEY is set) — best quality
  2. Tavily  (if TAVILY_API_KEY is set) — good for AI use cases
  3. DuckDuckGo HTML scraping — free fallback (existing logic, improved)

The provider is chosen automatically based on which API keys are available.
"""

import asyncio
import json
import logging
import os
import re
from typing import Any
from urllib.parse import quote_plus, unquote

import aiohttp

logger = logging.getLogger(__name__)

# ── Configuration ─────────────────────────────────────────────────────
SERPAPI_KEY = os.environ.get("SERPAPI_KEY", "")
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "")

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept-Encoding": "gzip, deflate",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


# ── Result Model ──────────────────────────────────────────────────────

class SearchResult:
    """Uniform search result across all providers."""

    def __init__(self, url: str, title: str, snippet: str, source: str = ""):
        self.url = url
        self.title = title
        self.snippet = snippet
        self.source = source

    def to_dict(self) -> dict[str, str]:
        return {
            "url": self.url,
            "title": self.title,
            "snippet": self.snippet,
            "source": self.source,
        }


# ── Provider 1: SerpAPI ──────────────────────────────────────────────

async def _search_serpapi(query: str, num_results: int = 10) -> list[SearchResult]:
    """Search using SerpAPI (Google results)."""
    if not SERPAPI_KEY:
        return []

    results = []
    try:
        params = {
            "q": query,
            "api_key": SERPAPI_KEY,
            "engine": "google",
            "num": str(num_results),
            "hl": "en",
        }
        url = "https://serpapi.com/search.json"
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url, params=params, timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                if resp.status != 200:
                    logger.warning("SerpAPI returned %d", resp.status)
                    return results
                data = await resp.json()

        for item in data.get("organic_results", [])[:num_results]:
            results.append(SearchResult(
                url=item.get("link", ""),
                title=item.get("title", ""),
                snippet=item.get("snippet", ""),
                source="serpapi",
            ))
    except Exception as exc:
        logger.warning("SerpAPI search failed: %s", exc)

    return results


# ── Provider 2: Tavily ───────────────────────────────────────────────

async def _search_tavily(query: str, num_results: int = 10) -> list[SearchResult]:
    """Search using Tavily API (AI-optimized search)."""
    if not TAVILY_API_KEY:
        return []

    results = []
    try:
        payload = {
            "api_key": TAVILY_API_KEY,
            "query": query,
            "search_depth": "basic",
            "max_results": num_results,
            "include_answer": False,
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://api.tavily.com/search",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    logger.warning("Tavily returned %d", resp.status)
                    return results
                data = await resp.json()

        for item in data.get("results", [])[:num_results]:
            results.append(SearchResult(
                url=item.get("url", ""),
                title=item.get("title", ""),
                snippet=item.get("content", "")[:300],
                source="tavily",
            ))
    except Exception as exc:
        logger.warning("Tavily search failed: %s", exc)

    return results


# ── Provider 3: DuckDuckGo (Free Fallback) ───────────────────────────

async def _search_duckduckgo(query: str, num_results: int = 10) -> list[SearchResult]:
    """Search using DuckDuckGo HTML scraping (free, no API key)."""
    results = []
    try:
        search_url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
        async with aiohttp.ClientSession() as session:
            async with session.get(
                search_url,
                headers=_HEADERS,
                timeout=aiohttp.ClientTimeout(total=12),
            ) as resp:
                if resp.status != 200:
                    return results
                html = await resp.text()

        # Parse result blocks — DuckDuckGo puts class before href
        result_blocks = re.findall(
            r'<a[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
            html,
            re.DOTALL,
        )
        snippets = re.findall(
            r'<a[^>]*class="result__snippet"[^>]*>(.*?)</a>',
            html,
            re.DOTALL,
        )

        for i, (url, title) in enumerate(result_blocks[:num_results]):
            real_url = url
            uddg_match = re.search(r'uddg=([^&]+)', url)
            if uddg_match:
                real_url = unquote(uddg_match.group(1))

            # Skip search engine URLs
            if any(
                d in real_url.lower()
                for d in ["duckduckgo.com", "google.com/search", "bing.com/search"]
            ):
                continue

            results.append(SearchResult(
                url=real_url,
                title=title.strip(),
                snippet=snippets[i].strip() if i < len(snippets) else "",
                source="duckduckgo",
            ))
    except Exception as exc:
        logger.debug("DuckDuckGo search failed: %s", exc)

    return results


# ── Unified Search Function ──────────────────────────────────────────

async def web_search(
    query: str,
    num_results: int = 10,
    provider: str = "auto",
) -> list[SearchResult]:
    """
    Universal search function with automatic provider selection.

    Priority: SerpAPI > Tavily > DuckDuckGo
    """
    if provider == "auto":
        if SERPAPI_KEY:
            provider = "serpapi"
        elif TAVILY_API_KEY:
            provider = "tavily"
        else:
            provider = "duckduckgo"

    logger.info("Searching [%s]: %s", provider, query)

    if provider == "serpapi":
        results = await _search_serpapi(query, num_results)
        if results:
            return results
        # Fallback to Tavily
        if TAVILY_API_KEY:
            results = await _search_tavily(query, num_results)
            if results:
                return results
        # Final fallback to DDG
        return await _search_duckduckgo(query, num_results)

    elif provider == "tavily":
        results = await _search_tavily(query, num_results)
        if results:
            return results
        return await _search_duckduckgo(query, num_results)

    else:
        return await _search_duckduckgo(query, num_results)


async def multi_search(
    queries: list[str],
    num_results_per_query: int = 5,
) -> list[SearchResult]:
    """
    Run multiple search queries in parallel and deduplicate results.
    """
    tasks = [web_search(q, num_results_per_query) for q in queries[:5]]
    all_results_lists = await asyncio.gather(*tasks, return_exceptions=True)

    seen_urls: set[str] = set()
    merged: list[SearchResult] = []

    for result_list in all_results_lists:
        if isinstance(result_list, Exception):
            logger.warning("Search query failed: %s", result_list)
            continue
        for r in result_list:
            if r.url not in seen_urls:
                seen_urls.add(r.url)
                merged.append(r)

    return merged


def get_search_status() -> dict[str, Any]:
    """Return the current search provider status."""
    providers = []
    active = "duckduckgo"

    if SERPAPI_KEY:
        providers.append({"name": "serpapi", "status": "configured"})
        active = "serpapi"
    else:
        providers.append({"name": "serpapi", "status": "not_configured"})

    if TAVILY_API_KEY:
        providers.append({"name": "tavily", "status": "configured"})
        if active == "duckduckgo":
            active = "tavily"
    else:
        providers.append({"name": "tavily", "status": "not_configured"})

    providers.append({"name": "duckduckgo", "status": "always_available"})

    return {
        "active_provider": active,
        "providers": providers,
    }
