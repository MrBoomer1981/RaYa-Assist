"""
obsidian_github.py — чтение vault через GitHub API.

Работает пока Mac выключен. Бот читает последний коммит.
Запись идёт через создание коммита напрямую в репо.

Настройка:
  GITHUB_TOKEN      = ghp_xxx...   (repo scope)
  GITHUB_VAULT_REPO = user/vault-repo-name
"""
from __future__ import annotations

import base64
import logging
from datetime import datetime

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_BASE = "https://api.github.com"


def _enabled() -> bool:
    return bool(
        getattr(settings, "github_token", "")
        and getattr(settings, "github_vault_repo", "")
    )


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {settings.github_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


async def read(path: str) -> str:
    """Читает файл из vault. path — относительный от корня репо."""
    if not _enabled():
        return ""
    try:
        url = f"{_BASE}/repos/{settings.github_vault_repo}/contents/{path}"
        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.get(url, headers=_headers())
        if r.status_code == 404:
            return ""
        r.raise_for_status()
        data = r.json()
        content = data.get("content", "")
        return base64.b64decode(content).decode("utf-8")
    except Exception as e:
        logger.debug("github read %s: %s", path, e)
        return ""


async def write(path: str, content: str, message: str = "") -> bool:
    """Создаёт или обновляет файл через коммит."""
    if not _enabled():
        return False
    try:
        url = f"{_BASE}/repos/{settings.github_vault_repo}/contents/{path}"
        encoded = base64.b64encode(content.encode()).decode()
        commit_msg = message or f"raya: update {path.split('/')[-1]}"

        # Нужен SHA если файл уже существует
        sha = None
        async with httpx.AsyncClient(timeout=8) as client:
            existing = await client.get(url, headers=_headers())
            if existing.status_code == 200:
                sha = existing.json().get("sha")

            payload: dict = {"message": commit_msg, "content": encoded}
            if sha:
                payload["sha"] = sha

            r = await client.put(url, headers=_headers(), json=payload)
            r.raise_for_status()
        return True
    except Exception as e:
        logger.warning("github write %s: %s", path, e)
        return False


async def search(query: str, limit: int = 10) -> list[dict]:
    """Поиск по vault через GitHub code search."""
    if not _enabled():
        return []
    try:
        url = f"{_BASE}/search/code"
        params = {
            "q": f"{query} repo:{settings.github_vault_repo}",
            "per_page": limit,
        }
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(url, headers=_headers(), params=params)
            r.raise_for_status()
        items = r.json().get("items", [])
        return [
            {
                "path":  i["path"],
                "filename": i["name"],
                "score": 1.0 - (idx * 0.05),
            }
            for idx, i in enumerate(items)
        ]
    except Exception as e:
        logger.debug("github search %r: %s", query, e)
        return []


async def list_folder(path: str = "") -> list[str]:
    """Список файлов в папке."""
    if not _enabled():
        return []
    try:
        url = f"{_BASE}/repos/{settings.github_vault_repo}/contents/{path}"
        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.get(url, headers=_headers())
            r.raise_for_status()
        items = r.json()
        if isinstance(items, list):
            return [i["path"] for i in items]
        return []
    except Exception as e:
        logger.debug("github list %s: %s", path, e)
        return []


async def ping() -> bool:
    """Проверяет доступность репо."""
    if not _enabled():
        return False
    try:
        url = f"{_BASE}/repos/{settings.github_vault_repo}"
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(url, headers=_headers())
        return r.status_code == 200
    except Exception:
        return False


# ── Высокоуровневые операции (те же что в obsidian.py) ────────────────────────

async def save_diary_entry(date: str, entry_text: str, mood: str) -> None:
    path    = f"📓 Дневник/{date}.md"
    now     = datetime.utcnow().strftime("%H:%M")
    block   = f"\n## {now} — {mood}\n\n{entry_text}\n"
    existing = await read(path)
    if not existing:
        content = f"# Дневник {date}\n\n> mood:: {mood}\n{block}"
    else:
        content = existing + block
    await write(path, content, f"diary: {date}")
    logger.info("📓 GitHub: дневник → %s", path)


async def save_calendar_day(date: str, events: list[dict]) -> None:
    path = f"📅 Расписание/{date}.md"
    if not events:
        content = f"# Расписание {date}\n\n_Событий нет_\n"
    else:
        lines = [f"# Расписание {date}\n"]
        for ev in sorted(events, key=lambda e: e.get("time_start") or ""):
            t = f" `{ev['time_start']}`" if ev.get("time_start") else ""
            lines.append(f"\n- {t} **{ev['title']}**")
        content = "\n".join(lines) + "\n"
    await write(path, content, f"calendar: {date}")


async def save_research_report(topic: str, report: str, sources: list, mode: str) -> str:
    date     = datetime.utcnow().strftime("%Y-%m-%d")
    filename = topic[:50].replace("/", "-").replace("\\", "-")
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
    await write(path, content, f"research: {filename}")
    return path


async def save_note(title: str, content: str, folder: str = "📝 Заметки") -> str:
    date     = datetime.utcnow().strftime("%Y-%m-%d %H:%M")
    filename = title[:60].replace("/", "-")
    path     = f"{folder}/{filename}.md"
    full     = f"# {title}\n\n> created:: {date}\n\n---\n\n{content}\n"
    await write(path, full, f"note: {filename}")
    return path


async def get_daily_context(date: str) -> str:
    diary = await read(f"📓 Дневник/{date}.md")
    sched = await read(f"📅 Расписание/{date}.md")
    parts = []
    if diary:
        parts.append(f"**Дневник {date}:**\n{diary[:800]}")
    if sched:
        parts.append(f"**Расписание {date}:**\n{sched[:600]}")
    return "\n\n".join(parts)
