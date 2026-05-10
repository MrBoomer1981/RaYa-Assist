"""
Async web scraper with Jina Reader fallback, deduplication, and source scoring.
"""
import asyncio
import hashlib
from typing import AsyncIterator, List, Optional, Set, Tuple

import aiohttp
from bs4 import BeautifulSoup

from deeper.services.cache_manager import CacheManager
from deeper.utils.logger import get_logger
from deeper.utils.text_utils import clean_html_text

logger = get_logger("web_scraper")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

JINA_HEADERS = {"Accept": "text/plain", "User-Agent": "Mozilla/5.0"}

SKIP_EXTENSIONS = {
    ".pdf", ".doc", ".docx", ".xls", ".xlsx",
    ".zip", ".gz", ".tar", ".rar",
    ".jpg", ".jpeg", ".png", ".gif", ".mp4", ".mp3",
}

# Sites known to block scrapers → go straight to Jina
JINA_PREFERRED = {
    "britannica.com", "medium.com", "forbes.com",
    "bloomberg.com", "wsj.com", "nytimes.com",
    "ft.com", "reuters.com", "economist.com",
}

# Source quality scoring — higher = more authoritative
SOURCE_SCORES = {
    # Tier 1 — highest authority
    "wikipedia.org": 10,
    "scholar.google.com": 10,
    "pubmed.ncbi.nlm.nih.gov": 10,
    "arxiv.org": 10,
    "nature.com": 10,
    "science.org": 10,
    # Tier 2 — educational & government
    ".edu": 8,
    ".gov": 8,
    "researchgate.net": 8,
    "jstor.org": 8,
    "springer.com": 8,
    "sciencedirect.com": 8,
    # Tier 3 — quality media & encyclopedias
    "britannica.com": 7,
    "reuters.com": 7,
    "bbc.com": 7,
    "theguardian.com": 6,
    "nytimes.com": 6,
    "plato.stanford.edu": 9,
    # Tier 4 — general (default)
}

DEFAULT_SCORE = 4


def score_url(url: str) -> int:
    """Return quality score for a URL. Higher = more authoritative."""
    url_lower = url.lower()
    best = DEFAULT_SCORE
    for domain, score in SOURCE_SCORES.items():
        if domain in url_lower:
            best = max(best, score)
    return best


def _is_scrapable(url: str) -> bool:
    lower = url.lower().split("?")[0]
    return not any(lower.endswith(ext) for ext in SKIP_EXTENSIONS)


def _needs_jina(url: str) -> bool:
    return any(domain in url for domain in JINA_PREFERRED)


def _content_hash(text: str) -> str:
    return hashlib.md5(text[:600].encode()).hexdigest()


def _extract_text(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "nav", "footer", "header",
                      "aside", "form", "noscript", "iframe", "svg"]):
        tag.decompose()
    main = (soup.find("article") or soup.find("main")
            or soup.find("div", {"id": "content"}))
    target = main if main else (soup.body if soup.body else soup)
    return clean_html_text(target.get_text(separator=" "))


class WebScraper:
    def __init__(self, cache: CacheManager, timeout: int = 15, retries: int = 3) -> None:
        self.cache = cache
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self.retries = retries

    async def _fetch_direct(self, session: aiohttp.ClientSession, url: str) -> Optional[str]:
        for attempt in range(1, self.retries + 1):
            try:
                async with session.get(
                    url, headers=HEADERS, timeout=self.timeout, allow_redirects=True
                ) as resp:
                    if resp.status != 200:
                        return None
                    ct = resp.headers.get("Content-Type", "")
                    if "text" not in ct and "html" not in ct:
                        return None
                    return await resp.text(errors="replace")
            except asyncio.TimeoutError:
                logger.debug("Timeout {} (attempt {})", url[:70], attempt)
            except Exception as e:
                logger.debug("Error {}: {}", url[:70], e)
                return None
            if attempt < self.retries:
                await asyncio.sleep(1.5 * attempt)
        return None

    async def _fetch_jina(self, session: aiohttp.ClientSession, url: str) -> Optional[str]:
        try:
            async with session.get(
                f"https://r.jina.ai/{url}",
                headers=JINA_HEADERS,
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                if resp.status != 200:
                    return None
                text = await resp.text(errors="replace")
                logger.info("Jina: {} chars from {}", len(text), url[:70])
                return text
        except Exception as e:
            logger.debug("Jina error {}: {}", url[:70], e)
            return None

    async def scrape(self, url: str) -> Optional[Tuple[str, int]]:
        """
        Scrape a URL. Returns (text, score) or None.
        Score reflects source authority.
        """
        if not _is_scrapable(url):
            return None

        score = score_url(url)
        cached = self.cache.get(url)
        if cached:
            return cached, score

        async with aiohttp.ClientSession() as session:
            text: Optional[str] = None
            if _needs_jina(url):
                text = await self._fetch_jina(session, url)
                if not text:
                    html = await self._fetch_direct(session, url)
                    text = _extract_text(html) if html else None
            else:
                html = await self._fetch_direct(session, url)
                if html:
                    text = _extract_text(html)
                if not text or len(text) < 100:
                    text = await self._fetch_jina(session, url)

        if not text or len(text) < 100:
            return None

        self.cache.set(url, text)
        logger.info("Scraped {} chars (score={}) from {}", len(text), score, url[:70])
        return text, score

    async def scrape_stream(
        self,
        urls: List[str],
        max_concurrent: int = 5,
    ) -> AsyncIterator[Tuple[str, int]]:
        """
        Async generator — yields (text, score) as each page finishes scraping.
        Enables parallel pipeline: analysis starts before all pages are done.
        Higher-scored sources are sorted first in the URL list.
        """
        # Sort URLs by score descending — authoritative sources scraped first
        sorted_urls = sorted(urls, key=score_url, reverse=True)

        semaphore = asyncio.Semaphore(max_concurrent)
        seen_hashes: Set[str] = set()
        queue: asyncio.Queue = asyncio.Queue()

        async def worker(url: str) -> None:
            async with semaphore:
                result = await self.scrape(url)
            await queue.put(result)

        tasks = [asyncio.create_task(worker(u)) for u in sorted_urls]
        completed = 0

        while completed < len(sorted_urls):
            result = await queue.get()
            completed += 1
            if result is None:
                continue
            text, score = result
            h = _content_hash(text)
            if h in seen_hashes:
                logger.debug("Duplicate page skipped")
                continue
            seen_hashes.add(h)
            yield text, score

        for task in tasks:
            task.cancel()

    async def scrape_many(self, urls: List[str], max_concurrent: int = 5) -> List[str]:
        """Legacy interface — returns list of texts, sorted by source score."""
        results: List[Tuple[str, int]] = []
        async for text, score in self.scrape_stream(urls, max_concurrent):
            results.append((text, score))
        results.sort(key=lambda x: x[1], reverse=True)
        logger.info("Scraped {}/{} pages", len(results), len(urls))
        return [t for t, _ in results]
