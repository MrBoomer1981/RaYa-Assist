"""
obsidian_tasks.py — синхронизация задач и идей с Obsidian vault.

Вызывается из todo_agent и ideas_agent после каждой мутации.
Vault — источник правды для отображения; SQLite — для логики.

Структура vault:
  📋 Задачи/Матрица.md        — Eisenhower board (перезаписывается целиком)
  📋 Задачи/Архив/YYYY-MM-DD.md — выполненные за день
  📝 Планы/Долгосрочные.md    — Q2 долгосрочные цели (append)
  📝 Планы/Идеи.md            — все идеи (append)
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
import time as _time

_last_sync: float = 0.0
_SYNC_DEBOUNCE_SEC = 30  # не обновляем матрицу чаще раза в 30 сек


logger = logging.getLogger(__name__)

# ── Константы ──────────────────────────────────────────────────────────────────

_MATRIX_PATH   = "📋 Задачи/Матрица.md"
_LONGTERM_PATH = "📝 Планы/Долгосрочные.md"
_IDEAS_PATH    = "📝 Планы/Идеи.md"

_Q_LABEL = {
    1: ("🔴 Q1 — Срочно и важно",    "Дедлайн горит. Сделать прямо сейчас."),
    2: ("🟡 Q2 — Важно, не срочно",  "Стратегия и развитие. Запланировать."),
    3: ("🟠 Q3 — Срочно, не важно",  "Мелкие просьбы. Делегировать или быстро закрыть."),
    4: ("⚪ Q4 — Не срочно, не важно","Шум. Исключить или отложить."),
}


# ── Публичные функции ──────────────────────────────────────────────────────────

async def sync_matrix(user_id: int) -> None:
    """
    Перезаписывает 📋 Задачи/Матрица.md актуальными задачами из SQLite.
    Вызывать после КАЖДОГО add/done/delete.
    """
    global _last_sync
    now = _time.monotonic()
    if now - _last_sync < _SYNC_DEBOUNCE_SEC:
        logger.debug("📋 Obsidian sync: debounce пропуск")
        return
    _last_sync = now

    from app.config import settings
    if not settings.obsidian_enabled:
        logger.warning(
            "📋 Obsidian sync пропущен — задай переменные в Railway:\n"
            "  GITHUB_TOKEN + GITHUB_VAULT_REPO (рекомендуется)\n"
            "  или OBSIDIAN_API_URL + OBSIDIAN_API_KEY"
        )
        return
    try:
        from app.database import get_active_tasks
        from app.services.obsidian import write

        tasks = get_active_tasks(user_id)
        content = _build_matrix(tasks)
        await write(_MATRIX_PATH, content)
        logger.info("📋 Obsidian: матрица обновлена (%d задач)", len(tasks))
    except Exception as e:
        logger.warning("obsidian_tasks.sync_matrix: %s", e)


async def archive_done(user_id: int, task_text: str, quadrant: int) -> None:
    """Добавляет выполненную задачу в архив дня."""
    from app.config import settings
    if not settings.obsidian_enabled:
        return
    try:
        from app.services.obsidian import append

        today     = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        path      = f"📋 Задачи/Архив/{today}.md"
        now_time  = datetime.now(timezone.utc).strftime("%H:%M")
        q_emoji   = _Q_LABEL.get(quadrant, _Q_LABEL[2])[0].split()[0]
        line      = f"\n- [x] {q_emoji} {task_text} ✅ {now_time}"

        await append(path, line)
        logger.info("📋 Obsidian: архив ← '%s'", task_text[:40])
    except Exception as e:
        logger.debug("obsidian_tasks.archive_done: %s", e)


async def add_longterm_goal(text: str, category: str = "") -> None:
    """Добавляет долгосрочную цель в 📝 Планы/Долгосрочные.md"""
    from app.config import settings
    if not settings.obsidian_enabled:
        return
    try:
        from app.services.obsidian import read, write

        existing = await read(_LONGTERM_PATH) or _longterm_template()
        date     = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        cat_tag  = f" #{category.lower().replace(' ', '_')}" if category else ""
        new_line = f"\n- [ ] {text}{cat_tag} `{date}`"

        # Вставляем в раздел ## Цели
        if "## Цели" in existing:
            existing = existing.replace("## Цели\n", f"## Цели\n{new_line}\n", 1)
        else:
            existing += f"\n{new_line}"

        await write(_LONGTERM_PATH, existing)
        logger.info("📝 Obsidian: долгосрочная цель ← '%s'", text[:40])
    except Exception as e:
        logger.debug("obsidian_tasks.add_longterm_goal: %s", e)


async def add_idea(text: str, context: str = "") -> None:
    """Добавляет идею в 📝 Планы/Идеи.md"""
    from app.config import settings
    if not settings.obsidian_enabled:
        return
    try:
        from app.services.obsidian import read, write

        existing = await read(_IDEAS_PATH) or _ideas_template()
        date     = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
        ctx_part = f"\n  > {context[:200]}" if context else ""
        new_block = f"\n\n### {text[:80]}\n`{date}`{ctx_part}\n"

        await write(_IDEAS_PATH, existing + new_block)
        logger.info("💡 Obsidian: идея ← '%s'", text[:40])
    except Exception as e:
        logger.debug("obsidian_tasks.add_idea: %s", e)


# ── Форматирование матрицы ─────────────────────────────────────────────────────

def _build_matrix(tasks: list[tuple]) -> str:
    """
    Строит Markdown-файл матрицы Эйзенхауэра из задач SQLite.
    tasks: [(id, text, priority, deadline), ...]
    priority 1=Q1, 2=Q2, 3=Q3, 4=Q4
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # Группируем по квадранту
    by_q: dict[int, list[tuple]] = {1: [], 2: [], 3: [], 4: []}
    for t in tasks:
        p = t[2]
        # priority 1=Q1, 2=Q2, 3=Q3, 4=Q4; остальное → Q2
        q = p if p in (1, 2, 3, 4) else 2
        by_q[q].append(t)

    lines = [
        "# 📋 Матрица Эйзенхауэра",
        f"\n> Обновлено: {now}  ",
        f"> Всего задач: {len(tasks)}\n",
    ]

    for q, (header, hint) in _Q_LABEL.items():
        lines.append(f"\n## {header}")
        lines.append(f"_{hint}_\n")
        if by_q[q]:
            for t in by_q[q]:
                task_id, text, _, deadline = t
                dl = f" 📅 `{deadline}`" if deadline else ""
                lines.append(f"- [ ] {text}{dl} <!-- id:{task_id} -->")
        else:
            lines.append("_Пусто_")

    lines.append(f"\n---\n_Синхронизировано RaYa_")
    return "\n".join(lines) + "\n"


def _longterm_template() -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return f"""# 📝 Долгосрочные планы

> Создано: {now}
> Обновляется автоматически через RaYa

---

## Цели

## Проекты

## Развитие

"""


def _ideas_template() -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return f"""# 💡 Идеи

> Создано: {now}
> Пополняется через RaYa автоматически

---

"""
