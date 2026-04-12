"""
search_service.py — поиск в интернете.

Три слоя:
  1. TTL-кэш (10 мин) — срезает повторные запросы при 25+ пользователях
  2. Tavily — основной движок (платный, качественный)
  3. DuckDuckGo — бесплатный fallback при ошибке Tavily или исчерпании квоты
"""
import asyncio
import hashlib
import logging
import time
from typing import Any

from app.config import settings

logger = logging.getLogger(__name__)

_SNIPPET_LEN  = 500   # символов на один результат (базовый поиск)
_DEEP_SNIPPET = 800   # для глубокого поиска / research


# ══════════════════════════════════════════════════════════════════════════════
# TTL-кэш
# ══════════════════════════════════════════════════════════════════════════════

class _SearchCache:
    """
    In-memory кэш поисковых результатов с TTL.
    При 25+ пользователях один популярный запрос (курс валют, погода)
    может прийти десятки раз за 10 минут — кэш срезает 60–80% реальных вызовов.
    """

    def __init__(self, ttl_sec: int = 600) -> None:
        self._ttl   = ttl_sec
        self._store: dict[str, tuple[float, Any]] = {}  # key → (expires_at, value)

    def _key(self, query: str, mode: str = "basic") -> str:
        raw = f"{mode}:{query.strip().lower()}"
        return hashlib.md5(raw.encode()).hexdigest()

    def get(self, query: str, mode: str = "basic") -> Any | None:
        k = self._key(query, mode)
        entry = self._store.get(k)
        if entry is None:
            return None
        expires_at, value = entry
        if time.monotonic() > expires_at:
            del self._store[k]
            return None
        logger.debug("search cache HIT: %s", query[:50])
        return value

    def set(self, query: str, value: Any, mode: str = "basic") -> None:
        if not value:
            return  # не кэшируем пустые результаты
        k = self._key(query, mode)
        self._store[k] = (time.monotonic() + self._ttl, value)

    def evict_expired(self) -> None:
        """Вызывается периодически для очистки устаревших записей."""
        now = time.monotonic()
        expired = [k for k, (exp, _) in self._store.items() if now > exp]
        for k in expired:
            del self._store[k]
        if expired:
            logger.debug("search cache: удалено %d устаревших записей", len(expired))


_cache = _SearchCache(ttl_sec=600)  # 10 минут


# ══════════════════════════════════════════════════════════════════════════════
# DuckDuckGo fallback
# ══════════════════════════════════════════════════════════════════════════════

async def _ddg_search(query: str, max_results: int = 3) -> list[dict]:
    """
    Бесплатный поиск через DuckDuckGo.
    Запускается в thread-pool чтобы не блокировать event loop.
    Возвращает список {title, content, url} или [] при ошибке.
    """
    try:
        from duckduckgo_search import DDGS

        def _sync_search():
            results = []
            with DDGS() as ddgs:
                for r in ddgs.text(query, max_results=max_results):
                    results.append({
                        "title":   r.get("title", ""),
                        "content": r.get("body",  "")[:_SNIPPET_LEN],
                        "url":     r.get("href",  ""),
                        "score":   0.5,  # DuckDuckGo не даёт score
                    })
            return results

        loop = asyncio.get_event_loop()
        results = await loop.run_in_executor(None, _sync_search)
        logger.info("🦆 DuckDuckGo fallback: %d результатов для '%s'", len(results), query[:50])
        return results

    except ImportError:
        logger.warning("duckduckgo-search не установлен — добавь в requirements.txt")
        return []
    except Exception as e:
        logger.warning("DDG search error: %s", e)
        return []


# ══════════════════════════════════════════════════════════════════════════════
# SearchService
# ══════════════════════════════════════════════════════════════════════════════

class SearchService:
    """
    Поиск с тремя слоями:
      cache → Tavily → DuckDuckGo fallback
    """

    def __init__(self) -> None:
        self._tavily_ok = True  # флаг: Tavily ещё работает
        try:
            from tavily import AsyncTavilyClient
            self._client = AsyncTavilyClient(api_key=settings.tavily_api_key)
        except Exception:
            logger.warning("Tavily недоступен — будет использован только DDG")
            self._client = None
            self._tavily_ok = False

    # ── Публичный API ─────────────────────────────────────────────────────────

    async def search(self, query: str, max_results: int = 3) -> str:
        """
        Базовый поиск → форматированный текст.
        Порядок: кэш → Tavily → DDG.
        """
        # 1. Кэш
        cached = _cache.get(query, "basic")
        if cached is not None:
            return cached

        results = await self._search_with_fallback(query, max_results, depth="basic")
        text = self._format(results, _SNIPPET_LEN)
        _cache.set(query, text, "basic")
        return text

    async def deep_search(self, query: str, max_results: int = 5) -> list[dict]:
        """
        Глубокий поиск → сырые результаты (для ResearchAgent).
        Порядок: кэш → Tavily advanced → DDG.
        """
        cached = _cache.get(query, "deep")
        if cached is not None:
            return cached

        results = await self._search_with_fallback(query, max_results, depth="advanced")
        _cache.set(query, results, "deep")
        return results

    async def multi_search(self, queries: list[str], max_per_query: int = 3) -> list[dict]:
        """
        Параллельно выполняет несколько запросов, дедуплицирует результаты.
        """
        if not queries:
            return []

        tasks = [self.deep_search(q, max_per_query) for q in queries[:5]]
        batches = await asyncio.gather(*tasks, return_exceptions=True)

        seen_urls: set[str] = set()
        merged: list[dict] = []
        for batch in batches:
            if isinstance(batch, Exception):
                continue
            for r in batch:
                url = r.get("url", "")
                if url and url not in seen_urls:
                    seen_urls.add(url)
                    merged.append(r)

        merged.sort(key=lambda x: x.get("score", 0), reverse=True)
        return merged[:10]

    async def fact_check(self, claim: str) -> str:
        """Ищет подтверждение и опровержение утверждения."""
        queries  = [claim, f"опровержение {claim}"]
        results  = await self.multi_search(queries, max_per_query=3)
        return self._format_raw(results, _DEEP_SNIPPET)

    # ── Внутренняя логика: Tavily → DDG ──────────────────────────────────────

    async def _search_with_fallback(
        self, query: str, max_results: int, depth: str
    ) -> list[dict]:
        """Пробует Tavily, при ошибке падает на DDG."""

        # Пробуем Tavily если он доступен
        if self._client and self._tavily_ok:
            try:
                results = await self._tavily_search(query, max_results, depth)
                if results:
                    return results
            except Exception as e:
                err_str = str(e).lower()
                if "429" in err_str or "quota" in err_str or "limit" in err_str:
                    logger.warning("⚠️ Tavily лимит исчерпан — переключаемся на DDG")
                    self._tavily_ok = False  # больше не пробуем Tavily
                else:
                    logger.warning("Tavily error: %s — fallback DDG", e)

        # DDG fallback
        return await _ddg_search(query, max_results)

    async def _tavily_search(
        self, query: str, max_results: int, depth: str
    ) -> list[dict]:
        """Запрос к Tavily API."""
        response = await self._client.search(
            query=query,
            max_results=max_results,
            search_depth=depth,
            include_raw_content=False,
        )
        snippet = _DEEP_SNIPPET if depth == "advanced" else _SNIPPET_LEN
        return [
            {
                "title":   r.get("title", ""),
                "content": r.get("content", "")[:snippet],
                "url":     r.get("url", ""),
                "score":   r.get("score", 0.0),
            }
            for r in response.get("results", [])
            if r.get("content")
        ]

    # ── Форматирование ────────────────────────────────────────────────────────

    @staticmethod
    def _format(results: list[dict], snippet_len: int) -> str:
        parts = []
        for r in results:
            title   = r.get("title",   "").strip()
            content = r.get("content", "").strip()[:snippet_len]
            if not content:
                continue
            chunk = []
            if title:
                chunk.append(f"[{title}]")
            chunk.append(content)
            parts.append("\n".join(chunk))
        return "\n\n".join(parts)

    @staticmethod
    def _format_raw(results: list[dict], snippet_len: int) -> str:
        """Форматирование с URL — для research_agent."""
        parts = []
        for r in results:
            title   = r.get("title",   "").strip()
            content = r.get("content", "").strip()[:snippet_len]
            url     = r.get("url",     "").strip()
            if not content:
                continue
            chunk = []
            if title:   chunk.append(f"[{title}]")
            chunk.append(content)
            if url:     chunk.append(f"Источник: {url}")
            parts.append("\n".join(chunk))
        return "\n\n".join(parts)
