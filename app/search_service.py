import asyncio
import logging
from typing import Any, Optional
from tavily import AsyncTavilyClient

from app.config import settings

logger = logging.getLogger(__name__)

_SNIPPET_LEN  = 500   # символов на один результат
_DEEP_SNIPPET = 800   # для глубокого поиска


class SearchService:
    """Поиск актуальной информации через Tavily."""

    def __init__(self) -> None:
        self._client = AsyncTavilyClient(api_key=settings.tavily_api_key)

    # ── Базовый поиск ─────────────────────────────────────────────────────────

    async def search(self, query: str, max_results: int = 3) -> str:
        """Один запрос → отформатированный текст результатов."""
        try:
            response = await self._client.search(
                query=query,
                max_results=max_results,
                search_depth="basic",
            )
            return self._format_results(response.get("results", []), _SNIPPET_LEN)
        except Exception:
            logger.exception("search: ошибка запроса '%s'", query[:60])
            return ""

    # ── Глубокий поиск одного запроса ────────────────────────────────────────

    async def deep_search(self, query: str, max_results: int = 5) -> list[dict]:
        """
        Возвращает сырые результаты с расширенным контентом.
        Используется research_agent для синтеза.
        """
        try:
            response = await self._client.search(
                query=query,
                max_results=max_results,
                search_depth="advanced",
                include_raw_content=False,
            )
            results = response.get("results", [])
            return [
                {
                    "title":   r.get("title", ""),
                    "content": r.get("content", "")[:_DEEP_SNIPPET],
                    "url":     r.get("url", ""),
                    "score":   r.get("score", 0.0),
                }
                for r in results if r.get("content")
            ]
        except Exception:
            logger.exception("deep_search: ошибка '%s'", query[:60])
            return []

    # ── Мультизапросный поиск ─────────────────────────────────────────────────

    async def multi_search(self, queries: list[str], max_per_query: int = 3) -> list[dict]:
        """
        Параллельно выполняет несколько запросов.
        Возвращает дедуплицированные результаты.
        """
        if not queries:
            return []

        tasks = [self.deep_search(q, max_per_query) for q in queries[:5]]
        all_results_nested = await asyncio.gather(*tasks, return_exceptions=True)

        seen_urls: set[str] = set()
        merged: list[dict] = []

        for batch in all_results_nested:
            if isinstance(batch, Exception):
                continue
            for r in batch:
                url = r.get("url", "")
                if url and url not in seen_urls:
                    seen_urls.add(url)
                    merged.append(r)

        # Сортируем по релевантности
        merged.sort(key=lambda x: x.get("score", 0), reverse=True)
        return merged[:10]

    # ── Проверка факта ────────────────────────────────────────────────────────

    async def fact_check(self, claim: str) -> str:
        """
        Ищет подтверждение или опровержение конкретного утверждения.
        Возвращает результаты с двух сторон.
        """
        queries = [
            claim,
            f"опровержение {claim}",
        ]
        results = await self.multi_search(queries, max_per_query=3)
        return self._format_results_raw(results, _DEEP_SNIPPET)

    # ── Форматирование ────────────────────────────────────────────────────────

    @staticmethod
    def _format_results(results: list[dict[str, Any]], snippet_len: int) -> str:
        parts = []
        for r in results:
            title   = r.get("title", "").strip()
            content = r.get("content", "").strip()[:snippet_len]
            url     = r.get("url", "").strip()
            if not content:
                continue
            chunk = []
            if title:   chunk.append(f"[{title}]")
            chunk.append(content)
            if url:     chunk.append(f"Источник: {url}")
            parts.append("\n".join(chunk))
        return "\n\n".join(parts)

    @staticmethod
    def _format_results_raw(results: list[dict], snippet_len: int) -> str:
        parts = []
        for r in results:
            title   = r.get("title", "").strip()
            content = r.get("content", "").strip()[:snippet_len]
            url     = r.get("url", "").strip()
            if not content:
                continue
            chunk = []
            if title: chunk.append(f"[{title}]")
            chunk.append(content)
            if url:   chunk.append(f"Источник: {url}")
            parts.append("\n".join(chunk))
        return "\n\n".join(parts)
