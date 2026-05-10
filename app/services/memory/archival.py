"""
Archival Memory — слой 3 из 3.

Безлимитный архив — никогда не вытесняется.
Поиск по запросу: не инжектируется автоматически, только по явному вызову.

Источники (в порядке приоритета):
  1. DEEper KnowledgeBase — результаты исследований
  2. Obsidian vault        — заметки, дневник (когда подключён)
  3. Загруженные документы — PDF/DOCX обработанные через document_service

Вызывается когда:
  - Пользователь явно спрашивает "что я изучал про X"
  - Recall вернул 0 результатов и запрос выглядит как поиск
  - Агент obsidian напрямую вызывает archival поиск
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


async def search(query: str, limit: int = 5) -> list[dict]:
    """
    Поиск по всем источникам архива.
    Возвращает список результатов {source, title, snippet, relevance}.
    """
    results: list[dict] = []

    # 1. DEEper KnowledgeBase
    deeper_results = await _search_deeper(query, limit=limit)
    results.extend(deeper_results)

    # 2. Obsidian vault
    obsidian_results = await _search_obsidian(query, limit=3)
    results.extend(obsidian_results)

    # Сортируем по релевантности (у DEEper — BM25 score, у Obsidian — score плагина)
    results.sort(key=lambda r: r.get("relevance", 0), reverse=True)
    return results[:limit]


async def _search_deeper(query: str, limit: int = 5) -> list[dict]:
    """Ищет в DEEper KnowledgeBase через bridge."""
    try:
        from app.agents.deep_research_agent import _get_bridge
        bridge = _get_bridge()
        raw = bridge.search_kb(query, limit=limit)
        results = []
        for r in raw:
            # DEEper возвращает Research dataclass или dict
            if hasattr(r, "title"):
                results.append({
                    "source":    "DEEper",
                    "title":     r.title,
                    "snippet":   r.summary[:300] if r.summary else "",
                    "relevance": getattr(r, "score", 0.5),
                    "id":        r.id,
                    "date":      r.formatted_timestamp() if hasattr(r, "formatted_timestamp") else "",
                })
            elif isinstance(r, dict):
                results.append({
                    "source":    "DEEper",
                    "title":     r.get("title", ""),
                    "snippet":   r.get("summary", "")[:300],
                    "relevance": r.get("score", 0.5),
                })
        return results
    except Exception as e:
        logger.debug("archival: DEEper search failed: %s", e)
        return []


async def _search_obsidian(query: str, limit: int = 3) -> list[dict]:
    """Ищет в Obsidian vault через obsidian service."""
    try:
        from app.config import settings
        if not settings.obsidian_enabled:
            return []
        from app.services.obsidian import search as obs_search
        raw = await obs_search(query, limit=limit)
        return [
            {
                "source":    "Obsidian",
                "title":     r.get("filename", r.get("path", "?")),
                "snippet":   r.get("content", "")[:300],
                "relevance": r.get("score", 0.4),
            }
            for r in raw
        ]
    except Exception as e:
        logger.debug("archival: Obsidian search failed: %s", e)
        return []


def format_for_prompt(results: list[dict], max_chars: int = 800) -> str:
    """Форматирует результаты архива для включения в промпт."""
    if not results:
        return ""
    lines = ["<archival_memory>"]
    total = 0
    for r in results:
        source  = r.get("source", "?")
        title   = r.get("title", "")
        snippet = r.get("snippet", "")
        date    = r.get("date", "")
        date_s  = f" [{date}]" if date else ""
        entry   = f"  [{source}]{date_s} {title}: {snippet}"
        if total + len(entry) > max_chars:
            break
        lines.append(entry)
        total += len(entry)
    lines.append("</archival_memory>")
    return "\n".join(lines)
