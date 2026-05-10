"""
obsidian_links.py — автоматические wiki-связи между заметками.

Вызывается после сохранения отчёта DEEper в vault.

Алгоритм:
  1. Из тем и ключевых фактов исследования строим поисковые запросы
  2. Ищем в vault существующие заметки через full-text search
  3. Фильтруем: убираем сам файл, слишком короткие совпадения, нерелевантные
  4. Добавляем секцию "## Связанные заметки" с [[wikilinks]] в новый отчёт
  5. В каждую связанную заметку добавляем обратную ссылку на отчёт

Формат в Obsidian:
  [[📓 Дневник/2025-01-15]]         — ссылка на конкретный файл
  [[Название без пути]]              — Obsidian сам найдёт по имени (если уникальное)

Использование:
  from app.services.obsidian_links import link_research_note
  await link_research_note(path, topic, topics, key_facts)
"""
from __future__ import annotations

import logging
import re
from pathlib import PurePosixPath

logger = logging.getLogger(__name__)

# Минимальный search-score для включения в связи
_MIN_SCORE     = 0.3
# Максимум связанных заметок (чтобы не засорять)
_MAX_LINKS     = 8
# Папки которые НЕ индексируем для связей (системные)
_SKIP_FOLDERS  = {"templates", "attachments", ".obsidian"}


async def link_research_note(
    new_path: str,
    topic: str,
    topics: list[str],
    key_facts: list[str],
) -> list[str]:
    """
    Находит связанные заметки и прописывает wiki-ссылки в обе стороны.

    Возвращает список путей файлов к которым добавлены обратные ссылки.
    """
    from app.services.obsidian import search, read, append

    if not topics and not key_facts:
        return []

    # ── 1. Строим поисковые запросы ──────────────────────────────────────────
    # Берём топ-3 темы + топ-2 ключевых факта (короткие фразы лучше ищутся)
    queries: list[str] = []
    for t in topics[:3]:
        queries.append(t.strip())
    for f in key_facts[:2]:
        # Берём первые 4 слова из факта
        short = " ".join(f.split()[:4])
        if short and short not in queries:
            queries.append(short)

    if not queries:
        queries = [topic[:50]]

    logger.info("🔗 Links: поиск по %d запросам для '%s'", len(queries), topic[:40])

    # ── 2. Поиск с дедупликацией ──────────────────────────────────────────────
    seen_paths: set[str] = set()
    candidates: list[dict] = []

    for q in queries:
        try:
            results = await search(q, limit=10)
        except Exception as e:
            logger.debug("Links search error q=%r: %s", q, e)
            continue

        for r in results:
            path = r.get("filename") or r.get("path") or ""
            if not path:
                continue

            # Нормализуем путь
            path = path.strip("/")

            # Пропускаем сам отчёт
            if path == new_path.strip("/"):
                continue

            # Пропускаем системные папки
            top_folder = path.split("/")[0].lower().strip("📓📅🔬📝")
            if any(skip in top_folder for skip in _SKIP_FOLDERS):
                continue

            if path in seen_paths:
                continue
            seen_paths.add(path)

            score = float(r.get("score", 0.0))
            candidates.append({"path": path, "score": score})

    # ── 3. Фильтрация и сортировка ────────────────────────────────────────────
    # score из Obsidian search plugin: выше = релевантнее
    candidates.sort(key=lambda x: x["score"], reverse=True)

    # Берём только с достаточным score
    filtered = [c for c in candidates if c["score"] >= _MIN_SCORE]

    # Если score нет (плагин вернул 0) — берём по порядку поиска
    if not filtered and candidates:
        filtered = candidates[:_MAX_LINKS]
    else:
        filtered = filtered[:_MAX_LINKS]

    if not filtered:
        logger.info("🔗 Links: связанных заметок не найдено")
        return []

    logger.info(
        "🔗 Links: найдено %d связанных заметок для '%s'",
        len(filtered), topic[:40],
    )

    # ── 4. Добавляем секцию в новый отчёт ────────────────────────────────────
    links_section = _build_links_section(filtered)
    try:
        await append(new_path, links_section)
        logger.info("🔗 Links: секция добавлена в %s", new_path)
    except Exception as e:
        logger.warning("🔗 Links: не удалось добавить секцию в %s: %s", new_path, e)
        return []

    # ── 5. Обратные ссылки в связанные заметки ────────────────────────────────
    backlinked: list[str] = []
    new_note_name = _note_name(new_path)
    backlink_block = _build_backlink(topic, new_note_name, new_path)

    for candidate in filtered:
        path = candidate["path"]
        try:
            existing = await read(path)
            # Не добавляем если обратная ссылка уже есть
            if new_note_name in existing or new_path in existing:
                backlinked.append(path)
                continue
            await append(path, backlink_block)
            backlinked.append(path)
            logger.debug("🔗 Backlink: %s ← %s", path, new_note_name)
        except Exception as e:
            logger.debug("🔗 Backlink error %s: %s", path, e)

    logger.info(
        "🔗 Links: обратные ссылки добавлены в %d файлов",
        len(backlinked),
    )
    return backlinked


# ── Форматирование ────────────────────────────────────────────────────────────

def _note_name(path: str) -> str:
    """Извлекает имя файла без расширения из пути."""
    return PurePosixPath(path).stem


def _build_links_section(candidates: list[dict]) -> str:
    """
    Строит секцию ## Связанные заметки для добавления в конец отчёта.

    Формат:
      ## Связанные заметки
      - [[📓 Дневник/2025-01-15]] — дневник
      - [[📝 Заметки/Идея про LLM]] — заметка
    """
    lines = ["\n\n## Связанные заметки\n"]
    for c in candidates:
        path  = c["path"]
        name  = _note_name(path)
        folder_emoji = _folder_label(path)
        # Используем полный путь для однозначной ссылки
        lines.append(f"- [[{path}|{name}]] — {folder_emoji}")
    return "\n".join(lines) + "\n"


def _build_backlink(topic: str, note_name: str, note_path: str) -> str:
    """
    Строит блок обратной ссылки для добавления в конец связанной заметки.

    Формат:
      ---
      🔬 Связано с исследованием: [[🔬 Исследования/2025-01-15 Тема|Тема]]
    """
    return (
        f"\n\n---\n"
        f"🔬 Связано с исследованием: [[{note_path}|{topic[:60]}]]\n"
    )


def _folder_label(path: str) -> str:
    """Возвращает читаемый тип папки по первому сегменту пути."""
    top = path.split("/")[0] if "/" in path else ""
    if "Дневник" in top or "📓" in top:
        return "дневник"
    if "Расписание" in top or "📅" in top:
        return "расписание"
    if "Исследования" in top or "🔬" in top:
        return "исследование"
    if "Заметки" in top or "📝" in top:
        return "заметка"
    return "заметка"
