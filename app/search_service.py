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
import math
import re
import time
from app.database import kc_get, kc_set, kc_cleanup
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

def _tfidf_vector(text: str) -> dict[str, float]:
    """Строит TF-IDF вектор для текста (без внешних библиотек)."""
    words = re.findall(r"[а-яёa-z]{3,}", text.lower())
    if not words:
        return {}
    tf: dict[str, float] = {}
    for w in words:
        tf[w] = tf.get(w, 0) + 1
    total = len(words)
    return {w: c / total for w, c in tf.items()}


def _cosine_sim(a: dict[str, float], b: dict[str, float]) -> float:
    """Cosine similarity между двумя TF-IDF векторами."""
    if not a or not b:
        return 0.0
    common = set(a) & set(b)
    if not common:
        return 0.0
    dot = sum(a[k] * b[k] for k in common)
    norm_a = math.sqrt(sum(v * v for v in a.values()))
    norm_b = math.sqrt(sum(v * v for v in b.values()))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def semantic_deduplicate(
    results: list[dict],
    threshold: float = 0.72,
) -> list[dict]:
    """
    #7 Семантическая дедупликация результатов поиска.

    Убирает дубли по смыслу (не только по URL).
    Три источника могут говорить одно и то же разными словами —
    достаточно оставить один + упомянуть сколько подтверждают.

    threshold=0.72 — хорошо отсекает парафразы, не трогает разные темы.
    """
    if len(results) <= 1:
        return results

    kept: list[dict] = []
    vectors: list[dict[str, float]] = []

    for r in results:
        text = (r.get("title", "") + " " + r.get("content", ""))
        vec = _tfidf_vector(text)
        # Сравниваем с уже отобранными
        is_dup = False
        for i, kv in enumerate(vectors):
            sim = _cosine_sim(vec, kv)
            if sim >= threshold:
                # Дубль — добавляем счётчик к оригиналу
                kept[i]["confirmed_by"] = kept[i].get("confirmed_by", 1) + 1
                is_dup = True
                break
        if not is_dup:
            r_copy = dict(r)
            r_copy["confirmed_by"] = 1
            kept.append(r_copy)
            vectors.append(vec)

    return kept


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
        # Чистим истёкшие записи knowledge cache при старте
        try:
            removed = kc_cleanup()
            if removed:
                logger.info("🗑️  knowledge_cache: удалено %d истёкших записей", removed)
        except Exception:
            pass

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

    async def news_search(self, query: str, max_results: int = 5) -> str:
        """
        #4 Специализированный поиск новостей через Tavily topic=news.
        Приоритизирует свежие новостные публикации над статичным контентом.
        Используется когда запрос явно про события/новости.
        """
        enriched = _enrich_query(query)

        cached = _cache_news.get(enriched, "news")
        if cached is not None:
            return cached

        results: list[dict] = []
        if self._client and self._tavily_ok:
            try:
                results = await self._tavily_search(
                    enriched, max_results, depth="advanced", topic="news"
                )
            except Exception as e:
                logger.warning("news_search Tavily error: %s → DDG fallback", e)

        if not results:
            results = await _ddg_search(enriched, max_results)

        text = _freshness_header() + self._format_raw(results, _DEEP_SNIPPET)
        _cache_news.set(enriched, text, "news")
        return text

    async def academic_search(self, query: str, max_results: int = 4) -> str:
        """
        #4 Специализированный поиск по академическим источникам.
        Добавляет site-hints к запросу чтобы ранжировать научные сайты выше.
        Используется research_agent в режиме science.
        """
        # Для академических запросов ищем на английском
        from app.search_service import _translate_key_terms
        en_query = _translate_key_terms(query)
        year = datetime.utcnow().year

        # Два запроса: обычный + academic-форматированный
        queries = [
            f"{en_query} site:arxiv.org OR site:pubmed.ncbi.nlm.nih.gov OR site:nature.com {year}",
            f"{en_query} research study {year}",
        ]

        all_results: list[dict] = []
        for q in queries:
            r = await self._search_with_fallback(q, max_results, depth="advanced")
            all_results.extend(r)

        # Дедупликация по URL
        seen: set[str] = set()
        merged = []
        for r in all_results:
            url = r.get("url", "")
            if url not in seen:
                seen.add(url)
                merged.append(r)

        merged.sort(key=lambda x: x.get("score", 0), reverse=True)
        return _freshness_header() + self._format_raw(merged[:6], _DEEP_SNIPPET)

    # ── Tavily → DDG ──────────────────────────────────────────────────────────

    async def smart_search(
        self,
        query: str,
        mode: str = "research",
        max_results: int = 5,
    ) -> str:
        """
        #6 Главный метод умного поиска: Search → Evaluate → Refine → Enrich.

        Полный pipeline:
          1. Выбирает специализированный метод по режиму (news / academic / general)
          2. Iterative search с авто-переформулированием если результаты нерелевантны
          3. Оценка финального качества через LLM (router_model)
          4. Full-page fetch топ-1 если нужно больше контекста
          5. Возвращает обогащённый текст с меткой свежести

        Используется research_agent вместо прямого вызова multi_search/iterative_search.
        """
        enriched_q = _enrich_query(query)

        # ── Шаг 0: проверяем persistent knowledge cache ───────────────────────
        kc_result = kc_get(query, mode)
        if kc_result:
            logger.info("📚 Knowledge cache HIT: '%s'", query[:50])
            return kc_result

        cache_store = _cache_news if self._is_event_query(enriched_q) else _cache
        cached = cache_store.get(f"smart:{enriched_q}", mode)
        if cached:
            return cached

        # ── Шаг 1: выбор специализированного метода ──────────────────────────
        is_news    = mode == "news" or self._is_event_query(enriched_q)
        is_science = mode == "science"

        if is_news:
            raw_text = await self.news_search(query, max_results)
            # Также парсим обратно в список для дальнейшей обработки
            results = await self._search_with_fallback(
                enriched_q, max_results, "advanced", topic="news"
            )
        elif is_science:
            raw_text = await self.academic_search(query, max_results)
            results = await self._search_with_fallback(enriched_q, max_results, "advanced")
        else:
            results = await self._search_with_fallback(enriched_q, max_results, "advanced")
            raw_text = ""

        # ── Шаг 2: итеративный поиск если результаты слабые ──────────────────
        if self._should_retry(query, results):
            refined_q = await self._refine_query(query, results)
            if refined_q and refined_q.lower() != enriched_q.lower():
                round2 = await self._search_with_fallback(
                    refined_q, max_results, "advanced",
                    topic="news" if is_news else "general",
                )
                # Объединяем и дедуплицируем
                seen: set[str] = {r.get("url","") for r in results}
                for r in round2:
                    if r.get("url","") not in seen:
                        results.append(r)
                        seen.add(r.get("url",""))
                logger.info("🔄 smart_search: round 2 refined='%s'", refined_q[:50])

        if not results:
            return _freshness_header() + "[Поиск не дал результатов]"

        results.sort(key=lambda x: x.get("score", 0), reverse=True)

        # ── Шаг 2.5: семантическая дедупликация ──────────────────────────────
        results = semantic_deduplicate(results, threshold=0.72)
        results.sort(key=lambda x: x.get("score", 0), reverse=True)

        # ── Шаг 3: LLM оценивает релевантность ───────────────────────────────
        quality = await self._evaluate_results(query, results[:3])
        if quality < 0.4 and not is_news:
            # Последняя попытка: переключаемся на английский
            from app.search_service import _translate_key_terms
            en_q = _translate_key_terms(query) + f" {datetime.utcnow().year}"
            if en_q.lower() != enriched_q.lower():
                en_results = await self._search_with_fallback(en_q, max_results, "advanced")
                if en_results:
                    results = (results + en_results)
                    results.sort(key=lambda x: x.get("score", 0), reverse=True)
                    logger.info("🌐 smart_search: EN fallback, quality was %.2f", quality)

        # ── Шаг 4: full-page fetch для топ-1 ─────────────────────────────────
        results = await self.enrich_top_result(results[:8])

        # ── Шаг 4.5: structured extraction для событийных запросов ───────────
        structured_block = ""
        if self._is_event_query(enriched_q) or mode in ("research", "news"):
            structured_block = await self.extract_event_facts(query, results[:4])

        # ── Шаг 5: финальное форматирование ──────────────────────────────────
        raw_text = self._format_raw(results, _DEEP_SNIPPET)
        if structured_block:
            text = _freshness_header() + structured_block + "\n\n" + raw_text
        else:
            text = _freshness_header() + raw_text
        cache_store.set(f"smart:{enriched_q}", text, mode)

        # Сохраняем в persistent cache: события — 2 ч, остальное — 24 ч
        ttl = 2 if self._is_event_query(enriched_q) else 24
        try:
            kc_set(query, text, mode, ttl_hours=ttl)
        except Exception as _kc_err:
            logger.debug("kc_set skipped: %s", _kc_err)

        return text

    async def _evaluate_results(self, query: str, results: list[dict]) -> float:
        """
        #6 Быстрая LLM-оценка релевантности результатов поиска.
        Возвращает float 0.0–1.0. Использует router_model (~0.2с).
        При ошибке — оптимистично возвращает 0.7 (не блокируем pipeline).
        """
        if not results:
            return 0.0
        try:
            from langchain_core.messages import HumanMessage, SystemMessage
            from langchain_groq import ChatGroq

            snippets = "\n".join(
                f"[{i+1}] {r.get('title','')} — {r.get('content','')[:120]}"
                for i, r in enumerate(results)
            )
            prompt = (
                f"Вопрос: {query}\n\n"
                f"Результаты поиска:\n{snippets}\n\n"
                "Насколько эти результаты отвечают на вопрос? "
                "Ответь ТОЛЬКО числом от 0.0 до 1.0 (0=совсем не релевантно, 1=идеально)."
            )
            llm = ChatGroq(
                api_key=settings.groq_api_key,
                model=settings.router_model,
                temperature=0.0,
            )
            resp = await llm.ainvoke([
                SystemMessage(content="Ты оцениваешь релевантность поисковых результатов. Отвечаешь только числом."),
                HumanMessage(content=prompt),
            ])
            score = float(str(resp.content).strip().replace(",", "."))
            score = max(0.0, min(1.0, score))
            logger.debug("search quality score=%.2f for '%s'", score, query[:50])
            return score
        except Exception as e:
            logger.debug("_evaluate_results error: %s", e)
            return 0.7  # оптимистичный fallback

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

    async def _fetch_full_page(self, url: str, timeout: int = 6) -> str:
        """
        #5 Скачивает и извлекает чистый текст страницы через httpx + trafilatura.
        Используется для топ-1 результата чтобы дать LLM полный контекст статьи.
        Возвращает до 3000 символов очищенного текста или "" при ошибке.
        """
        if not url or not url.startswith("http"):
            return ""
        try:
            import httpx
            try:
                import trafilatura
                has_trafilatura = True
            except ImportError:
                has_trafilatura = False

            async with httpx.AsyncClient(
                timeout=timeout,
                follow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0 (compatible; RaYaBot/1.0)"},
            ) as client:
                resp = await client.get(url)
                if resp.status_code != 200:
                    return ""
                html = resp.text

            if has_trafilatura:
                text = trafilatura.extract(
                    html,
                    include_comments=False,
                    include_tables=False,
                    no_fallback=False,
                )
            else:
                # Fallback: грубая очистка тегов
                import re
                text = re.sub(r"<[^>]+>", " ", html)
                text = re.sub(r"\s+", " ", text).strip()

            if not text:
                return ""

            # Берём самый релевантный кусок — первые 3000 символов
            return text[:3000].strip()

        except Exception as e:
            logger.debug("_fetch_full_page failed for %s: %s", url[:60], e)
            return ""

    async def enrich_top_result(self, results: list[dict]) -> list[dict]:
        """
        #5 Обогащает топ-1 результат полным текстом страницы.
        Остальные результаты остаются со сниппетами.
        Запускается только если сниппет топ-1 содержит менее 300 символов.
        """
        if not results:
            return results

        top = results[0]
        if len(top.get("content", "")) >= 300:
            return results  # сниппет достаточно длинный — не тратим время

        full_text = await self._fetch_full_page(top.get("url", ""))
        if full_text:
            enriched = dict(top)
            enriched["content"] = full_text
            enriched["full_page"] = True
            logger.info("📄 Full-page fetch: %s (%d chars)", top.get("url", "")[:60], len(full_text))
            return [enriched] + results[1:]

        return results

    async def extract_event_facts(
        self, query: str, results: list[dict]
    ) -> str:
        """
        #8 Структурированное извлечение фактов для событийных запросов.

        Если запрос про событие/миссию/релиз — просим router_model
        извлечь ключевые поля: дата, статус, участники, место.
        Даёт LLM чёткие факты вместо пересказа сниппетов.
        Используется только когда _is_event_query() == True.
        """
        if not results:
            return ""
        try:
            from langchain_core.messages import HumanMessage, SystemMessage
            from langchain_groq import ChatGroq

            snippets = "\n\n".join(
                f"[{r.get('title','')}]\n{r.get('content','')[:300]}"
                for r in results[:4]
            )

            prompt = (
                f"Вопрос: {query}\n\n"
                f"Данные из поиска:\n{snippets}\n\n"
                "Извлеки структурированные факты из текста выше. "
                "Верни JSON с полями (оставь null если данных нет):\n"
                '{"status": "текущий статус", '
                '"date": "дата события или дедлайн", '
                '"location": "место", '
                '"participants": "участники/организации", '
                '"key_facts": ["факт1", "факт2", "факт3"]}'
            )
            llm = ChatGroq(
                api_key=settings.groq_api_key,
                model=settings.router_model,
                temperature=0.0,
            )
            resp = await llm.ainvoke([
                SystemMessage(content="Ты извлекаешь структурированные факты из текста. Отвечаешь только JSON."),
                HumanMessage(content=prompt),
            ])
            raw = str(resp.content).strip().strip("```json").strip("```").strip()
            data = json.loads(raw)

            # Форматируем в читаемый блок для LLM-агента
            lines = ["[Структурированные факты]"]
            if data.get("status"):
                lines.append(f"Статус: {data['status']}")
            if data.get("date"):
                lines.append(f"Дата: {data['date']}")
            if data.get("location"):
                lines.append(f"Место: {data['location']}")
            if data.get("participants"):
                lines.append(f"Участники: {data['participants']}")
            if data.get("key_facts"):
                lines.append("Ключевые факты:")
                for f in data["key_facts"]:
                    if f:
                        lines.append(f"  • {f}")

            structured = "\n".join(lines)
            logger.info("📋 Structured extraction: %d fields for '%s'", len(data), query[:50])
            return structured

        except Exception as e:
            logger.debug("extract_event_facts error: %s", e)
            return ""

    async def _search_with_fallback(
        self, query: str, max_results: int, depth: str,
        topic: str = "general",
    ) -> list[dict]:
        if self._client and self._tavily_ok:
            try:
                results = await self._tavily_search(query, max_results, depth, topic=topic)
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
        self, query: str, max_results: int, depth: str,
        topic: str = "general",
    ) -> list[dict]:
        kwargs: dict = {
            "query":              query,
            "max_results":        max_results,
            "search_depth":       depth,
            "include_raw_content": False,
        }
        # Tavily поддерживает topic="news" — приоритизирует свежие новостные сайты
        if topic == "news":
            kwargs["topic"] = "news"
        response = await self._client.search(**kwargs)
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
            title        = r.get("title",        "").strip()
            content      = r.get("content",      "").strip()[:snippet_len]
            url          = r.get("url",          "").strip()
            published    = r.get("published",    "").strip()
            confirmed_by = r.get("confirmed_by", 1)
            if not content:
                continue
            chunk = []
            header = f"[{title}]" if title else ""
            if published:
                header += f" ({published})"
            if confirmed_by > 1:
                header += f" ✓×{confirmed_by}"  # N источников говорят одно
            if header:
                chunk.append(header)
            chunk.append(content)
            if url:
                chunk.append(f"Источник: {url}")
            parts.append("\n".join(chunk))
        return "\n\n".join(parts)


# ══════════════════════════════════════════════════════════════════════════════
# SocialSearchService — Reddit и X (Twitter) через Tavily + DDG fallback
# ══════════════════════════════════════════════════════════════════════════════

# Топовые сабреддиты по категориям для поиска трендов
_REDDIT_SUBS = {
    "tech":    "technology OR MachineLearning OR programming OR singularity",
    "news":    "worldnews OR news OR geopolitics",
    "science": "science OR space OR Futurology",
    "finance": "investing OR CryptoCurrency OR Economics OR stocks",
    "general": "popular OR all",
}

# Категории тем X/Twitter для поиска
_X_TOPICS = {
    "tech":    "AI OR ChatGPT OR tech OR programming",
    "news":    "breaking OR news OR world",
    "finance": "crypto OR stocks OR bitcoin OR economy",
}


class SocialSearchService:
    """
    Поиск горячих обсуждений на Reddit и X (Twitter).

    Стратегия:
    - Tavily с site:reddit.com / site:x.com (краулит напрямую, обходит блокировки)
    - DDG с site: фильтром как fallback
    - Результаты дедублицируются и сортируются по свежести
    """

    def __init__(self) -> None:
        self._search = SearchService()

    async def reddit_hot(
        self,
        topic: str = "tech",
        query: str = "",
        max_results: int = 5,
    ) -> list[dict]:
        """
        Горячие посты Reddit по теме или произвольному запросу.
        Возвращает список dict: {title, summary, url, source='reddit'}.
        """
        subreddits = _REDDIT_SUBS.get(topic, _REDDIT_SUBS["general"])

        if query:
            search_q = f"site:reddit.com {query}"
        else:
            year = datetime.utcnow().year
            search_q = f"site:reddit.com ({subreddits}) hot discussion {year}"

        raw = await self._search.news_search(search_q, max_results=max_results)
        return _parse_social_results(raw, source="reddit")

    async def x_trending(
        self,
        topic: str = "tech",
        query: str = "",
        max_results: int = 5,
    ) -> list[dict]:
        """
        Трендовые посты X (Twitter) по теме или запросу.
        Возвращает список dict: {title, summary, url, source='x'}.
        """
        x_filter = _X_TOPICS.get(topic, query or "trending")

        if query:
            search_q = f"site:x.com OR site:twitter.com {query}"
        else:
            year = datetime.utcnow().year
            search_q = f"(site:x.com OR site:twitter.com) {x_filter} {year}"

        raw = await self._search.news_search(search_q, max_results=max_results)
        return _parse_social_results(raw, source="x")

    async def trending_digest(
        self,
        topics: list[str] | None = None,
        reddit_count: int = 3,
        x_count: int = 2,
    ) -> str:
        """
        Полный дайджест трендов: Reddit + X по заданным темам.
        Используется в morning_agent и по запросу пользователя.
        """
        if topics is None:
            topics = ["tech", "news"]

        tasks = []
        labels = []
        for t in topics:
            tasks.append(self.reddit_hot(topic=t, max_results=reddit_count))
            labels.append(("reddit", t))
            tasks.append(self.x_trending(topic=t, max_results=x_count))
            labels.append(("x", t))

        results = await asyncio.gather(*tasks, return_exceptions=True)

        reddit_items: list[dict] = []
        x_items: list[dict] = []

        for (source, _topic), result in zip(labels, results):
            if isinstance(result, Exception) or not result:
                continue
            if source == "reddit":
                reddit_items.extend(result)
            else:
                x_items.extend(result)

        # Дедупликация по URL
        seen: set[str] = set()
        reddit_items = _dedup(reddit_items, seen)
        x_items      = _dedup(x_items,      seen)

        parts = []
        if reddit_items:
            lines = ["🔴 **Горячее на Reddit:**"]
            for item in reddit_items[:5]:
                lines.append(f"• {item['title']}")
                if item.get("summary"):
                    lines.append(f"  _{item['summary'][:120]}_")
            parts.append("\n".join(lines))

        if x_items:
            lines = ["🐦 **Трендовое на X:**"]
            for item in x_items[:4]:
                lines.append(f"• {item['title']}")
                if item.get("summary"):
                    lines.append(f"  _{item['summary'][:100]}_")
            parts.append("\n".join(lines))

        if not parts:
            return ""

        header = f"[Данные получены: {datetime.utcnow().strftime('%d.%m.%Y %H:%M')} UTC]\n"
        return header + "\n\n".join(parts)

    async def search_reddit(self, query: str, max_results: int = 5) -> str:
        """Произвольный поиск по Reddit — для прямых запросов пользователя."""
        items = await self.reddit_hot(query=query, max_results=max_results)
        if not items:
            return "Ничего не нашла на Reddit по этому запросу."
        lines = [f"🔴 **Reddit — '{query}':**\n"]
        for item in items:
            lines.append(f"• **{item['title']}**")
            if item.get("summary"):
                lines.append(f"  {item['summary'][:150]}")
            if item.get("url"):
                lines.append(f"  {item['url']}")
            lines.append("")
        return "\n".join(lines)

    async def search_x(self, query: str, max_results: int = 5) -> str:
        """Произвольный поиск по X/Twitter — для прямых запросов пользователя."""
        items = await self.x_trending(query=query, max_results=max_results)
        if not items:
            return "Ничего не нашла на X по этому запросу."
        lines = [f"🐦 **X — '{query}':**\n"]
        for item in items:
            lines.append(f"• **{item['title']}**")
            if item.get("summary"):
                lines.append(f"  {item['summary'][:150]}")
            lines.append("")
        return "\n".join(lines)


# ── Вспомогательные функции ───────────────────────────────────────────────────

def _parse_social_results(raw_text: str, source: str) -> list[dict]:
    """Парсит текстовый вывод SearchService в список структур."""
    if not raw_text:
        return []
    items = []
    # Разбиваем по блокам (разделены пустой строкой)
    blocks = [b.strip() for b in raw_text.split("\n\n") if b.strip()]
    for block in blocks:
        lines = block.splitlines()
        if not lines:
            continue
        title   = lines[0].strip("[]").strip()
        summary = " ".join(lines[1:]).strip() if len(lines) > 1 else ""
        # Убираем метку источника если есть
        url = ""
        for line in lines:
            if line.startswith("Источник:"):
                url = line.replace("Источник:", "").strip()
                summary = summary.replace(line, "").strip()
        # Пропускаем служебные строки (timestamp freshness header)
        if title.startswith("[Данные получены"):
            continue
        if len(title) < 5:
            continue
        items.append({"title": title[:200], "summary": summary[:300], "url": url, "source": source})
    return items


def _dedup(items: list[dict], seen: set) -> list[dict]:
    result = []
    for item in items:
        key = item.get("url") or item.get("title", "")[:60]
        if key and key not in seen:
            seen.add(key)
            result.append(item)
    return result
