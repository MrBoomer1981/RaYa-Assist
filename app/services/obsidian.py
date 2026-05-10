"""
app/services/obsidian.py — клиент для obsidian-local-rest-api.

Документация плагина: https://github.com/coddingtonbear/obsidian-local-rest-api

Все публичные функции — async, возвращают результат или бросают ObsidianError.
Остальной код (агенты) использует только эти функции — никакого HTTP снаружи.

Структура vault:
  📅 Расписание/YYYY-MM-DD.md       ← calendar_agent
  📓 Дневник/YYYY-MM-DD.md          ← diary_agent (совместим с Daily Notes)
  🔬 Исследования/YYYY-MM-DD Тема.md ← deep_research_agent
  📝 Заметки/название.md            ← obsidian_agent (произвольные заметки)
"""
from __future__ import annotations

import logging
import re
import ssl
from datetime import datetime
from typing import Any

import aiohttp

from app.config import settings

logger = logging.getLogger(__name__)


class ObsidianError(Exception):
    """Базовый класс ошибок Obsidian-сервиса."""


def _client() -> aiohttp.ClientSession:
    """
    Создаём сессию с отключённой проверкой SSL —
    плагин использует self-signed сертификат.
    """
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE
    connector = aiohttp.TCPConnector(ssl=ssl_ctx)
    return aiohttp.ClientSession(
        base_url=settings.obsidian_api_url.rstrip("/"),
        headers={
            "Authorization": f"Bearer {settings.obsidian_api_key}",
            "Content-Type":  "text/markdown",
        },
        connector=connector,
        timeout=aiohttp.ClientTimeout(total=10),
    )


def _enabled() -> bool:
    return bool(settings.obsidian_api_url and settings.obsidian_api_key)


def _safe_filename(s: str, max_len: int = 60) -> str:
    """Очищает строку для использования в имени файла."""
    s = re.sub(r'[\\/:*?"<>|#^[\]{}]', "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:max_len] or "заметка"


# ── Низкоуровневые операции ───────────────────────────────────────────────────

async def read(path: str) -> str:
    """Читает файл из vault. path — относительный от корня vault."""
    if not _enabled():
        raise ObsidianError("Obsidian не настроен")
    async with _client() as s:
        async with s.get(f"/vault/{path}") as r:
            if r.status == 404:
                return ""
            if r.status != 200:
                raise ObsidianError(f"read {path}: HTTP {r.status}")
            return await r.text()


async def write(path: str, content: str) -> None:
    """Создаёт или полностью перезаписывает файл."""
    if not _enabled():
        raise ObsidianError("Obsidian не настроен")
    async with _client() as s:
        async with s.put(f"/vault/{path}", data=content.encode()) as r:
            if r.status not in (200, 204):
                raise ObsidianError(f"write {path}: HTTP {r.status}")


async def append(path: str, content: str) -> None:
    """Дописывает контент в конец файла (создаёт если нет)."""
    if not _enabled():
        raise ObsidianError("Obsidian не настроен")
    async with _client() as s:
        async with s.post(f"/vault/{path}", data=content.encode()) as r:
            if r.status not in (200, 204):
                raise ObsidianError(f"append {path}: HTTP {r.status}")


async def delete(path: str) -> bool:
    """Удаляет файл. Возвращает True если удалён, False если не существовал."""
    if not _enabled():
        raise ObsidianError("Obsidian не настроен")
    async with _client() as s:
        async with s.delete(f"/vault/{path}") as r:
            if r.status == 404:
                return False
            if r.status not in (200, 204):
                raise ObsidianError(f"delete {path}: HTTP {r.status}")
            return True


async def list_folder(path: str = "") -> list[str]:
    """Возвращает список файлов в папке (только имена, рекурсивно)."""
    if not _enabled():
        raise ObsidianError("Obsidian не настроен")
    folder = path.rstrip("/") + "/" if path else ""
    async with _client() as s:
        async with s.get(f"/vault/{folder}") as r:
            if r.status != 200:
                raise ObsidianError(f"list {path}: HTTP {r.status}")
            data = await r.json()
            return data.get("files", [])


async def search(query: str, limit: int = 20) -> list[dict]:
    """Полнотекстовый поиск по vault."""
    if not _enabled():
        raise ObsidianError("Obsidian не настроен")
    async with _client() as s:
        async with s.post(
            "/search/simple/",
            headers={"Content-Type": "application/json"},
            json={"query": query},
        ) as r:
            if r.status != 200:
                raise ObsidianError(f"search: HTTP {r.status}")
            data = await r.json()
            results = data if isinstance(data, list) else data.get("results", [])
            return results[:limit]


async def ping() -> bool:
    """Проверяет доступность плагина. Не бросает исключений."""
    if not _enabled():
        return False
    try:
        async with _client() as s:
            async with s.get("/") as r:
                return r.status == 200
    except Exception:
        return False


# ── Высокоуровневые операции (используют агенты) ─────────────────────────────

async def save_diary_entry(date: str, entry_text: str, mood: str) -> None:
    """
    Добавляет запись дневника в Daily Note: 📓 Дневник/YYYY-MM-DD.md
    Совместим с плагином Daily Notes в Obsidian.
    """
    path = f"📓 Дневник/{date}.md"
    now  = datetime.utcnow().strftime("%H:%M")

    block = f"\n## {now} — {mood}\n\n{entry_text}\n"

    existing = await read(path)
    if not existing:
        header = f"# Дневник {date}\n\n> mood:: {mood}\n"
        await write(path, header + block)
    else:
        # Обновляем mood:: в frontmatter если хуже/лучше
        await append(path, block)

    logger.info("📓 Obsidian: запись дневника → %s", path)


async def save_calendar_day(date: str, events: list[dict]) -> None:
    """
    Перезаписывает файл расписания на день: 📅 Расписание/YYYY-MM-DD.md
    Вызывается после любого изменения событий.
    """
    path = f"📅 Расписание/{date}.md"

    if not events:
        content = f"# Расписание {date}\n\n_Событий нет_\n"
    else:
        lines = [f"# Расписание {date}\n"]
        for ev in sorted(events, key=lambda e: e.get("time_start") or ""):
            time_part = f" `{ev['time_start']}`" if ev.get("time_start") else ""
            time_end  = f"–{ev['time_end']}" if ev.get("time_end") else ""
            color_tag = f" #{ev['color']}" if ev.get("color") and ev["color"] != "blue" else ""
            desc      = f"\n  > {ev['description']}" if ev.get("description") else ""
            lines.append(f"\n- [{ev.get('id','')}]{time_part}{time_end} **{ev['title']}**{color_tag}{desc}")
        content = "\n".join(lines) + "\n"

    await write(path, content)
    logger.info("📅 Obsidian: расписание → %s (%d событий)", path, len(events))


async def save_research_report(topic: str, report: str, sources: list, mode: str) -> str:
    """
    Сохраняет отчёт DEEper: 🔬 Исследования/YYYY-MM-DD Тема.md
    Возвращает путь к файлу.
    """
    date     = datetime.utcnow().strftime("%Y-%m-%d")
    filename = _safe_filename(topic)
    path     = f"🔬 Исследования/{date} {filename}.md"

    sources_block = ""
    if sources:
        sources_block = "\n## Источники\n\n" + "\n".join(
            f"- [{s.get('title', s.get('url', '?'))}]({s.get('url', '')})"
            for s in sources[:20]
        )

    content = (
        f"# {topic}\n\n"
        f"> mode:: {mode}  \n"
        f"> date:: {date}  \n"
        f"> sources:: {len(sources)}\n\n"
        f"---\n\n"
        f"{report}"
        f"{sources_block}\n"
    )

    await write(path, content)
    logger.info("🔬 Obsidian: исследование → %s", path)
    return path


async def save_note(title: str, content: str, folder: str = "📝 Заметки") -> str:
    """
    Сохраняет произвольную заметку.
    Возвращает путь к файлу.
    """
    date     = datetime.utcnow().strftime("%Y-%m-%d %H:%M")
    filename = _safe_filename(title)
    path     = f"{folder}/{filename}.md"

    full_content = f"# {title}\n\n> created:: {date}\n\n---\n\n{content}\n"
    await write(path, full_content)
    logger.info("📝 Obsidian: заметка → %s", path)
    return path


async def get_daily_context(date: str) -> str:
    """
    Читает дневник + расписание на дату.
    Используется утренним дайджестом для персонализации.
    """
    diary   = await read(f"📓 Дневник/{date}.md")
    sched   = await read(f"📅 Расписание/{date}.md")
    parts   = []
    if diary:
        parts.append(f"**Дневник {date}:**\n{diary[:800]}")
    if sched:
        parts.append(f"**Расписание {date}:**\n{sched[:600]}")
    return "\n\n".join(parts)
