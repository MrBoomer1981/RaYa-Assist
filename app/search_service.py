import logging
from typing import Any
from tavily import AsyncTavilyClient

from app.config import settings

logger = logging.getLogger(__name__)

# Максимальная длина одного результата поиска (символов)
_RESULT_SNIPPET_LEN = 400


class SearchService:
    """Сервис для поиска актуальной информации в интернете через Tavily."""

    def __init__(self) -> None:
        self._client = AsyncTavilyClient(api_key=settings.tavily_api_key)

    @staticmethod
    def _format_result(r: dict[str, Any]) -> str | None:
        """Форматирует один результат поиска в читаемую строку."""
        title = r.get("title", "").strip()
        content = r.get("content", "").strip()[:_RESULT_SNIPPET_LEN]
        url = r.get("url", "").strip()

        # Пропускаем пустые результаты
        if not content:
            return None

        parts = []
        if title:
            parts.append(f"[{title}]")
        parts.append(content)
        if url:
            parts.append(f"Источник: {url}")
        return "\n".join(parts)

    async def search(self, query: str) -> str:
        """
        Ищет информацию по запросу.
        Возвращает отформатированный текст с результатами
        или пустую строку если ничего не найдено / произошла ошибка.
        """
        try:
            response = await self._client.search(
                query=query,
                max_results=3,
                search_depth="basic",
            )
            results: list[dict[str, Any]] = response.get("results", [])
            if not results:
                logger.info("Поиск '%s' — результатов нет", query)
                return ""

            parts = [
                formatted
                for r in results
                if (formatted := self._format_result(r)) is not None
            ]
            return "\n\n".join(parts) if parts else ""

        except Exception:
            logger.exception("Ошибка поиска по запросу: '%s'", query)
            return ""
