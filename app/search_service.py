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
import json
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


# Темы где английский поиск даёт лучше результаты
_INTERNATIONAL_KW = (
    "nasa", "spacex", "artemis", "артемид", "starship", "falcon",
    "openai", "chatgpt", "anthropic", "claude", "gemini", "gpt",
    "илон маск", "elon musk", "apple", "google", "microsoft",
    "tesla", "youtube", "twitter", "instagram", "tiktok",
    "who", "воз", "imf", "мвф", "un ", "оон",
    "olympics", "олимпиад", "world cup", "чм ", "чемпионат мира",
    "nobel", "нобел",
)

_RUSSIAN_TO_ENGLISH: dict[str, str] = {
    "артемида": "artemis",
    "роскосмос": "roscosmos",
    "луна": "moon",
    "марс": "mars",
    "космос": "space",
    "запуск": "launch",
    "миссия": "mission",
}


def _is_international(query: str) -> bool:
    """Тема международная — английский поиск будет точнее."""
    q = query.lower()
    return any(kw in q for kw in _INTERNATIONAL_KW)


def _translate_key_terms(query: str) -> str:
    """Заменяет ключевые русские термины английскими для лучшего поиска."""
    result = query
    for ru, en in _RUSSIAN_TO_ENGLISH.items():
        result = result.replace(ru, en)
    return result


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

    async def iterative_search(
        self,
        query: str,
        max_results: int = 5,
        max_iterations: int = 2,
    ) -> str:
        """
        Итеративный поиск: если первый раунд даёт мало релевантного контента,
        LLM (router_model) переформулирует запрос и ищет снова.

        Алгоритм:
          1. Поиск по исходному запросу
          2. Быстрая оценка релевантности (router_model, ~0.2с)
          3. Если релевантность низкая — переформулировать + 2-й поиск
          4. Объединить результаты обоих раундов

        Используется research_agent и raya_agent для сложных запросов.
        """
        enriched = _enrich_query(query)
        cache_store = _cache_news if self._is_event_query(enriched) else _cache
        cache_key = f"iterative:{enriched}"

        cached = cache_store.get(cache_key, "iter")
        if cached is not None:
            return cached

        # ── Раунд 1 ───────────────────────────────────────────────────────────
        results = await self._search_with_fallback(enriched, max_results, depth="advanced")
        
        if not results:
            return _freshness_header() + "[Поиск не дал результатов]"

        # ── Оценка релевантности ──────────────────────────────────────────────
        round2_results: list[dict] = []
        if max_iterations > 1 and self._should_retry(query, results):
            refined_query = await self._refine_query(query, results)
            if refined_query and refined_query.lower() != enriched.lower():
                logger.info(
                    "🔄 Iterative search: round 2 | original='%s' refined='%s'",
                    query[:50], refined_query[:50],
                )
                round2_results = await self._search_with_fallback(
                    refined_query, max_results, depth="advanced"
                )

        # ── Объединить и дедуплицировать ──────────────────────────────────────
        seen_urls: set[str] = set()
        merged: list[dict] = []
        for r in results + round2_results:
            url = r.get("url", "")
            if url not in seen_urls:
                seen_urls.add(url)
                merged.append(r)

        merged.sort(key=lambda x: x.get("score", 0), reverse=True)
        text = _freshness_header() + self._format_raw(merged[:8], _DEEP_SNIPPET)

        cache_store.set(cache_key, text, "iter")
        return text

    def _should_retry(self, query: str, results: list[dict]) -> bool:
        """
        Эвристика: стоит ли делать второй раунд поиска.
        Retry если: мало результатов ИЛИ суммарный контент короткий.
        """
        if len(results) < 2:
            return True
        total_content = sum(len(r.get("content", "")) for r in results)
        if total_content < 400:
            return True
        # Проверяем что ключевые слова из запроса встречаются в результатах
        query_words = set(query.lower().split())
        query_words -= {"что", "как", "где", "когда", "почему", "кто", "the", "a", "is"}
        if not query_words:
            return False
        all_content = " ".join(r.get("content", "").lower() for r in results)
        matched = sum(1 for w in query_words if w in all_content)
        coverage = matched / len(query_words) if query_words else 1.0
        should = coverage < 0.4  # менее 40% слов запроса найдено в результатах
        if should:
            logger.debug("iterative: low coverage %.0f%% → retry", coverage * 100)
        return should

    async def _refine_query(self, original: str, results: list[dict]) -> str:
        """
        Просит router_model переформулировать запрос на основе того
        что нашли в первом раунде — точнее попасть во втором.
        """
        try:
            from langchain_core.messages import HumanMessage, SystemMessage
            from langchain_groq import ChatGroq

            snippets = "\n".join(
                f"- {r.get('title', '')}: {r.get('content', '')[:150]}"
                for r in results[:3]
            )
            prompt = (
                f"Оригинальный запрос: {original}\n\n"
                f"Найденные результаты (нерелевантные или неполные):\n{snippets}\n\n"
                "Придумай ОДИН альтернативный поисковый запрос который найдёт более точную информацию.\n"
                "Можно использовать английский если тема международная.\n"
                "Ответь ТОЛЬКО запросом, без пояснений."
            )
            llm = ChatGroq(
                api_key=settings.groq_api_key,
                model=settings.router_model,
                temperature=0.2,
            )
            response = await llm.ainvoke([
                SystemMessage(content="Ты эксперт по поисковым запросам. Отвечаешь только самим запросом."),
                HumanMessage(content=prompt),
            ])
            refined = str(response.content).strip().strip('"').strip("'")
            return refined[:200]  # защита от слишком длинных запросов
        except Exception as e:
            logger.debug("_refine_query error: %s", e)
            return ""

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
        results = []
        for r in response.get("results", []):
            if not r.get("content"):
                continue
            score = float(r.get("score", 0.0))
            # Буст за свежесть: если в тексте/заголовке есть текущий год — +0.15
            current_year = str(datetime.utcnow().year)
            text_combined = (r.get("title", "") + r.get("content", "")).lower()
            if current_year in text_combined:
                score += 0.15
            # Штраф за старые года (2+ лет назад)
            for old_year in range(2020, datetime.utcnow().year - 1):
                if str(old_year) in text_combined and current_year not in text_combined:
                    score -= 0.1
                    break
            results.append({
                "title":        r.get("title", ""),
                "content":      r.get("content", "")[:snippet],
                "url":          r.get("url", ""),
                "score":        score,
                "published":    r.get("published_date", ""),
            })
        # Сортируем по итоговому score
        results.sort(key=lambda x: x["score"], reverse=True)
        return results

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
            title     = r.get("title",     "").strip()
            content   = r.get("content",   "").strip()[:snippet_len]
            url       = r.get("url",       "").strip()
            published = r.get("published", "").strip()
            if not content:
                continue
            chunk = []
            header = f"[{title}]" if title else ""
            if published:
                header += f" ({published})"
            if header:
                chunk.append(header)
            chunk.append(content)
            if url:
                chunk.append(f"Источник: {url}")
            parts.append("\n".join(chunk))
        return "\n\n".join(parts)
