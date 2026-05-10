"""
Tavily-based web search wrapper.
Extracts and forwards AI answers alongside URLs for richer report context.
"""
import asyncio
from typing import Dict, List

from tavily import TavilyClient

from deeper.utils.logger import get_logger

logger = get_logger("web_search")


class WebSearch:
    def __init__(self, api_key: str, pages_per_query: int = 2) -> None:
        self.client = TavilyClient(api_key=api_key)
        self.pages_per_query = pages_per_query

    async def search_query(self, query: str) -> Dict:
        """Run a single Tavily search. Returns urls, snippets, and AI answer."""
        try:
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None,
                lambda: self.client.search(
                    query=query,
                    search_depth="advanced",
                    max_results=self.pages_per_query,
                    include_answer=True,
                    include_raw_content=False,
                ),
            )

            urls: List[str] = []
            snippets: List[str] = []
            answers: List[str] = []

            # Collect Tavily's own AI answer — high-quality distilled insight
            tavily_answer = result.get("answer", "").strip()
            if tavily_answer and len(tavily_answer) > 30:
                answers.append(f"[Tavily об '{query}']: {tavily_answer}")

            for item in result.get("results", []):
                url = item.get("url", "")
                snippet = item.get("content", "").strip()
                if url:
                    urls.append(url)
                if snippet:
                    snippets.append(f"[{url}] {snippet}")

            logger.info("Query '{}' → {} URLs, answer: {}",
                        query[:50], len(urls), "да" if tavily_answer else "нет")

            return {"urls": urls, "snippets": snippets, "answers": answers}

        except Exception as e:
            logger.error("Search error for '{}': {}", query[:50], e)
            return {"urls": [], "snippets": [], "answers": []}

    async def search_many(self, queries: List[str], max_concurrent: int = 3) -> Dict[str, Dict]:
        """Run multiple queries concurrently."""
        semaphore = asyncio.Semaphore(max_concurrent)

        async def guarded(q: str) -> tuple:
            async with semaphore:
                return q, await self.search_query(q)

        pairs = await asyncio.gather(*[guarded(q) for q in queries])
        results = {q: r for q, r in pairs}

        total_urls = sum(len(r["urls"]) for r in results.values())
        total_answers = sum(len(r["answers"]) for r in results.values())
        logger.info("{} запросов → {} URLs, {} AI-ответов Tavily",
                    len(queries), total_urls, total_answers)
        return results
