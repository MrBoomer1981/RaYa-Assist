"""
search_service.py — поиск в интернете.

Три слоя:
  1. TTL-кэш (10 мин) — срезает повторные запросы при 25+ пользователях
  2. Tavily — основной движок (платный, качественный)
  3. DuckDuckGo — бесплатный fallback при ошибке Tavily или исчерпании квоты

Свежесть данных:
  - _enrich_query() добавляет год к time-sensitive запросам
  - _freshness_header() добавляет временную метку к результатам
  - LLM видит «Данные получены: 12.04.2026 16:40 UTC» и не путает с архивными данными
"""
import asyncio
import hashlib
import logging
import time
from datetime import datetime
from typing import Any

from app.config import settings

logger = logging.getLogger(__name__)

_SNIPPET_LEN  = 500   # символов на один результат
_DEEP_SNIPPET = 800   # для глубокого поиска / research

# Ключевые слова — если встречаются в запросе, добавляем год
_TIME_SENSITIVE_KW = (
    # Время и актуальность
    "курс", "цена", "стоимость", "погода", "новост", "сейчас", "сегодня",
    "актуальн", "последн", "вышел", "вышла", "выпустил", "обновлен",
    "версия", "релиз", "price", "rate", "news", "today", "latest", "current",
    # События и миссии
    "запуск", "миссия", "полёт", "полет", "старт", "дата", "когда",
    "статус", "результат", "итог", "произошло", "случилось",
    "launch", "mission", "status", "schedule", "update",
    # Конкретные проекты (часто спрашивают об их статусе)
    "артемид", "artemis", "spacex", "starship", "falcon",
    "chatgpt", "gpt", "gemini", "claude", "openai", "anthropic",
)


def _enrich_query(query: str) -> str:
    """
    Обогащает запрос для получения свежих результатов:
    - Добавляет год если тема чувствительна ко времени
    - Для event-запросов добавляет "latest" если на английском
    'курс доллара' → 'курс доллара 2026'
    'Artemis 2' → 'Artemis 2 2026'
    """
    q_lower = query.lower()
    year = str(datetime.utcnow().year)
    prev_year = str(int(year) - 1)
    # Уже содержит год — не дублируем
    if year in query or prev_year in query:
        return query
    if any(kw in q_lower for kw in _TIME_SENSITIVE_KW):
        return f"{query} {year}"
    return query


def _freshness_header() -> str:
    """Временная метка получения данных — добавляется в начало результатов."""
    return f"[Данные получены: {datetime.utcnow().strftime('%d.%m.%Y %H:%M')} UTC]\n"


# ══════════════════════════════════════════════════════════════════════════════
# TTL-кэш
# ══════════════════════════════════════════════════════════════════════════════

class _SearchCache:
    """
    In-memory кэш с TTL. При 25+ пользователях популярные запросы
    (курс валют, погода) приходят десятки раз — кэш срезает 60–80% вызовов.
    """
    def __init__(self, ttl_sec: int = 600) -> None:
        self._ttl   = ttl_sec
        self._store: dict[str, tuple[float, Any]] = {}

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
            return
        k = self._key(query, mode)
        self._store[k] = (time.monotonic() + self._ttl, value)

    def evict_expired(self) -> None:
        now = time.monotonic()
        expired = [k for k, (exp, _) in self._store.items() if now > exp]
        for k in expired:
            del self._store[k]


_cache      = _SearchCache(ttl_sec=600)    # обычные запросы — 10 мин
_cache_news = _SearchCache(ttl_sec=120)   # новости/события — 2 мин


# ══════════════════════════════════════════════════════════════════════════════
# DuckDuckGo fallback
# ══════════════════════════════════════════════════════════════════════════════

async def _ddg_search(query: str, max_results: int = 3) -> list[dict]:
    """
    Бесплатный поиск через DuckDuckGo.
    Запускается в thread-pool чтобы не блокировать event loop.
    """
    try:
        from duckduckgo_search import DDGS

        def _sync():
            results = []
            with DDGS() as ddgs:
                for r in ddgs.text(query, max_results=max_results):
                    results.append({
                        "title":   r.get("title", ""),
                        "content": r.get("body",  "")[:_SNIPPET_LEN],
                        "url":     r.get("href",  ""),
                        "score":   0.5,
                    })
            return results

        loop = asyncio.get_event_loop()
        results = await loop.run_in_executor(None, _sync)
        logger.info("🦆 DuckDuckGo: %d результатов для '%s'", len(results), query[:50])
        return results

    except ImportError:
        logger.warning("duckduckgo-search не установлен — добавь в requirements.txt")
        return []
    except Exception as e:
        logger.warning("DDG error: %s", e)
        return []


# ══════════════════════════════════════════════════════════════════════════════
# SearchService
# ══════════════════════════════════════════════════════════════════════════════

class SearchService:
    """
    Поиск с тремя слоями: кэш → Tavily → DuckDuckGo.
    Автоматически обогащает запросы датой и добавляет метку свежести.
    """

    def __init__(self) -> None:
        self._tavily_ok = True
        try:
            from tavily import AsyncTavilyClient
            self._client = AsyncTavilyClient(api_key=settings.tavily_api_key)
        except Exception:
            logger.warning("Tavily недоступен — только DDG")
            self._client    = None
            self._tavily_ok = False

    # ── Публичный API ─────────────────────────────────────────────────────────

    def _is_event_query(self, query: str) -> bool:
        """Новостные/событийные запросы кэшируем короче."""
        q = query.lower()
        event_kw = ("новост", "запуск", "миссия", "статус", "artemis", "spacex",
                    "news", "launch", "latest", "update", "today", "сегодня")
        return any(kw in q for kw in event_kw)

    async def search(self, query: str, max_results: int = 3) -> str:
        """
        Базовый поиск → форматированный текст с меткой свежести.
        Порядок: кэш → Tavily → DDG.
        Событийные запросы кэшируются 2 мин вместо 10.
        """
        enriched = _enrich_query(query)
        cache_store = _cache_news if self._is_event_query(enriched) else _cache

        cached = cache_store.get(enriched, "basic")
        if cached is not None:
            return cached

        results = await self._search_with_fallback(enriched, max_results, depth="basic")
        text = _freshness_header() + self._format(results, _SNIPPET_LEN)
        cache_store.set(enriched, text, "basic")
        return text

    async def deep_search(self, query: str, max_results: int = 5) -> list[dict]:
        """
        Глубокий поиск → сырые результаты (для ResearchAgent).
        """
        enriched = _enrich_query(query)

        cached = _cache.get(enriched, "deep")
        if cached is not None:
            return cached

        results = await self._search_with_fallback(enriched, max_results, depth="advanced")
        _cache.set(enriched, results, "deep")
        return results

    async def multi_search(self, queries: list[str], max_per_query: int = 3) -> list[dict]:
        """
        Параллельно выполняет несколько запросов, дедуплицирует результаты.
        Каждый запрос обогащается датой независимо.
        """
        if not queries:
            return []

        tasks   = [self.deep_search(q, max_per_query) for q in queries[:5]]
        batches = await asyncio.gather(*tasks, return_exceptions=True)

        seen_urls: set[str] = set()
        merged: list[dict]  = []
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
        queries = [claim, f"опровержение {claim}"]
        results = await self.multi_search(queries, max_per_query=3)
        return _freshness_header() + self._format_raw(results, _DEEP_SNIPPET)

    # ── Tavily → DDG ──────────────────────────────────────────────────────────

    async def _search_with_fallback(
        self, query: str, max_results: int, depth: str
    ) -> list[dict]:
        if self._client and self._tavily_ok:
            try:
                results = await self._tavily_search(query, max_results, depth)
                if results:
                    return results
            except Exception as e:
                err = str(e).lower()
                if "429" in err or "quota" in err or "limit" in err:
                    logger.warning("⚠️ Tavily лимит — переключаемся на DDG")
                    self._tavily_ok = False
                else:
                    logger.warning("Tavily error: %s — fallback DDG", e)

        return await _ddg_search(query, max_results)

    async def _tavily_search(
        self, query: str, max_results: int, depth: str
    ) -> list[dict]:
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
