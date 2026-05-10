"""
obsidian.py — фасад для работы с Obsidian vault.

Два бэкенда (выбирается автоматически):
  1. GitHub API   — если GITHUB_TOKEN + GITHUB_VAULT_REPO заданы
                    работает всегда, даже когда Mac выключен
  2. Local REST   — если OBSIDIAN_API_URL + OBSIDIAN_API_KEY заданы
                    прямой доступ через плагин obsidian-local-rest-api

Приоритет: GitHub > Local REST (GitHub надёжнее для облачного бота).
Все вызовы снаружи идут через этот файл — бэкенд прозрачен.
"""
from __future__ import annotations

import logging
from app.config import settings

logger = logging.getLogger(__name__)


def _backend():
    """Возвращает активный бэкенд как модуль."""
    if settings.obsidian_via_github:
        from app.services import obsidian_github as _gh
        return _gh
    return None  # будем использовать прямые функции ниже


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



# ── Публичные функции — роутер к активному бэкенду ───────────────────────────

async def read(path: str) -> str:
    b = _backend()
    if b:
        return await b.read(path)
    if not _enabled(): return ""
    async with _client() as s:
        async with s.get(f"/vault/{path}") as r:
            if r.status == 404: return ""
            r.raise_for_status()
            return await r.text()


async def write(path: str, content: str) -> None:
    b = _backend()
    if b:
        await b.write(path, content)
        return
    if not _enabled(): raise ObsidianError("Obsidian не настроен")
    async with _client() as s:
        async with s.put(f"/vault/{path}", data=content.encode()) as r:
            if r.status not in (200, 204): raise ObsidianError(f"write {path}: HTTP {r.status}")


async def append(path: str, content: str) -> None:
    b = _backend()
    if b:
        existing = await b.read(path)
        await b.write(path, existing + content)
        return
    if not _enabled(): raise ObsidianError("Obsidian не настроен")
    async with _client() as s:
        async with s.post(f"/vault/{path}", data=content.encode()) as r:
            if r.status not in (200, 204): raise ObsidianError(f"append {path}: HTTP {r.status}")


async def delete(path: str) -> bool:
    b = _backend()
    if b:
        logger.warning("GitHub backend: delete не поддерживается напрямую")
        return False
    if not _enabled(): raise ObsidianError("Obsidian не настроен")
    async with _client() as s:
        async with s.delete(f"/vault/{path}") as r:
            if r.status == 404: return False
            if r.status not in (200, 204): raise ObsidianError(f"delete {path}: HTTP {r.status}")
            return True


async def list_folder(path: str = "") -> list[str]:
    b = _backend()
    if b:
        return await b.list_folder(path)
    if not _enabled(): raise ObsidianError("Obsidian не настроен")
    folder = path.rstrip("/") + "/" if path else ""
    async with _client() as s:
        async with s.get(f"/vault/{folder}") as r:
            if r.status != 200: raise ObsidianError(f"list {path}: HTTP {r.status}")
            data = await r.json()
            return data.get("files", [])


async def search(query: str, limit: int = 20) -> list[dict]:
    b = _backend()
    if b:
        return await b.search(query, limit)
    if not _enabled(): raise ObsidianError("Obsidian не настроен")
    async with _client() as s:
        async with s.post("/search/simple/",
                          headers={"Content-Type": "application/json"},
                          json={"query": query}) as r:
            if r.status != 200: raise ObsidianError(f"search: HTTP {r.status}")
            data = await r.json()
            results = data if isinstance(data, list) else data.get("results", [])
            return results[:limit]


async def ping() -> bool:
    b = _backend()
    if b:
        return await b.ping()
    if not _enabled(): return False
    try:
        async with _client() as s:
            async with s.get("/") as r:
                return r.status == 200
    except Exception:
        return False


# ── Высокоуровневые операции ──────────────────────────────────────────────────

async def save_diary_entry(date: str, entry_text: str, mood: str) -> None:
    b = _backend()
    if b:
        await b.save_diary_entry(date, entry_text, mood)
        return
    from datetime import datetime as _dt
    path     = f"📓 Дневник/{date}.md"
    now      = _dt.utcnow().strftime("%H:%M")
    block    = f"\n## {now} — {mood}\n\n{entry_text}\n"
    existing = await read(path)
    if not existing:
        await write(path, f"# Дневник {date}\n\n> mood:: {mood}\n{block}")
    else:
        await append(path, block)


async def save_calendar_day(date: str, events: list[dict]) -> None:
    b = _backend()
    if b:
        await b.save_calendar_day(date, events)
        return
    path = f"📅 Расписание/{date}.md"
    if not events:
        content = f"# Расписание {date}\n\n_Событий нет_\n"
    else:
        lines = [f"# Расписание {date}\n"]
        for ev in sorted(events, key=lambda e: e.get("time_start") or ""):
            t = f" `{ev['time_start']}`" if ev.get("time_start") else ""
            lines.append(f"\n- {t} **{ev['title']}**")
        content = "\n".join(lines) + "\n"
    await write(path, content)


async def save_research_report(topic: str, report: str, sources: list, mode: str) -> str:
    b = _backend()
    if b:
        return await b.save_research_report(topic, report, sources, mode)
    from datetime import datetime as _dt
    import re as _re
    date     = _dt.utcnow().strftime("%Y-%m-%d")
    filename = _re.sub(r'[\\/:*?"<>|#^\[\]{}]', "", topic)[:60].strip()
    path     = f"🔬 Исследования/{date} {filename}.md"
    sources_block = ""
    if sources:
        sources_block = "\n## Источники\n\n" + "\n".join(
            f"- [{s.get('title', s.get('url', '?'))}]({s.get('url', '')})"
            for s in sources[:20]
        )
    content = (
        f"# {topic}\n\n"
        f"> mode:: {mode}  \n> date:: {date}  \n> sources:: {len(sources)}\n\n---\n\n"
        f"{report}{sources_block}\n"
    )
    await write(path, content)
    return path


async def save_note(title: str, content: str, folder: str = "📝 Заметки") -> str:
    b = _backend()
    if b:
        return await b.save_note(title, content, folder)
    from datetime import datetime as _dt
    import re as _re
    date     = _dt.utcnow().strftime("%Y-%m-%d %H:%M")
    filename = _re.sub(r'[\\/:*?"<>|#^\[\]{}]', "", title)[:60].strip()
    path     = f"{folder}/{filename}.md"
    await write(path, f"# {title}\n\n> created:: {date}\n\n---\n\n{content}\n")
    return path


async def get_daily_context(date: str) -> str:
    b = _backend()
    if b:
        return await b.get_daily_context(date)
    diary = await read(f"📓 Дневник/{date}.md")
    sched = await read(f"📅 Расписание/{date}.md")
    parts = []
    if diary: parts.append(f"**Дневник {date}:**\n{diary[:800]}")
    if sched: parts.append(f"**Расписание {date}:**\n{sched[:600]}")
    return "\n\n".join(parts)
