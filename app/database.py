"""
database.py — работа с SQLite.
WAL-режим, connection-per-call через контекстный менеджер.
"""
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Generator

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

logger = logging.getLogger(__name__)

DB_PATH = Path("database.db")
_TIME_FMT = "%Y-%m-%d %H:%M:%S"


@contextmanager
def _conn() -> Generator[sqlite3.Connection, None, None]:
    """Контекстный менеджер соединения с авто-commit/rollback."""
    con = sqlite3.connect(DB_PATH, detect_types=sqlite3.PARSE_DECLTYPES)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA foreign_keys=ON")
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def init_db() -> None:
    """Создаёт таблицы, индексы и настраивает БД."""
    with _conn() as con:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS history (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER NOT NULL,
                role       TEXT    NOT NULL CHECK(role IN ('human', 'ai')),
                content    TEXT    NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS user_memory (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER NOT NULL,
                fact       TEXT    NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS reminders (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER NOT NULL,
                text       TEXT    NOT NULL,
                remind_at  DATETIME NOT NULL,
                done       INTEGER  NOT NULL DEFAULT 0,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS diary (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER NOT NULL,
                entry      TEXT    NOT NULL,
                mood       TEXT    DEFAULT '',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_history_user
                ON history(user_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_memory_user
                ON user_memory(user_id);
            CREATE INDEX IF NOT EXISTS idx_reminders_due
                ON reminders(remind_at, done);
            CREATE INDEX IF NOT EXISTS idx_diary_user
                ON diary(user_id, created_at);
        """)
    # Таблица задач
        con.executescript("""
            CREATE TABLE IF NOT EXISTS tasks (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER NOT NULL,
                text       TEXT    NOT NULL,
                priority   INTEGER NOT NULL DEFAULT 2,  -- 1=высокий, 2=средний, 3=низкий
                due_date   TEXT    DEFAULT '',
                done       INTEGER NOT NULL DEFAULT 0,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS mood_log (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER NOT NULL,
                mood       TEXT    NOT NULL,
                context    TEXT    DEFAULT '',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_tasks_user
                ON tasks(user_id, done);
            CREATE INDEX IF NOT EXISTS idx_mood_user
                ON mood_log(user_id, created_at);
        """)
    logger.info("✅ База данных готова: %s", DB_PATH)


# ── История ───────────────────────────────────────────────────────────────────

def load_history(user_id: int, limit: int = 20) -> list[BaseMessage]:
    """Загружает последние N сообщений в хронологическом порядке."""
    with _conn() as con:
        rows = con.execute("""
            SELECT role, content FROM (
                SELECT role, content, created_at FROM history
                WHERE user_id = ?
                ORDER BY created_at DESC LIMIT ?
            ) ORDER BY created_at ASC
        """, (user_id, limit)).fetchall()
    return [
        HumanMessage(content=c) if r == "human" else AIMessage(content=c)
        for r, c in rows
    ]


def save_messages(user_id: int, human: str, ai: str) -> None:
    """Сохраняет пару сообщений одной транзакцией."""
    with _conn() as con:
        con.executemany(
            "INSERT INTO history (user_id, role, content) VALUES (?, ?, ?)",
            [(user_id, "human", human), (user_id, "ai", ai)],
        )


def clear_history(user_id: int) -> None:
    with _conn() as con:
        con.execute("DELETE FROM history WHERE user_id = ?", (user_id,))


# ── Память ────────────────────────────────────────────────────────────────────

def load_memory(user_id: int) -> list[str]:
    with _conn() as con:
        rows = con.execute(
            "SELECT fact FROM user_memory WHERE user_id = ? ORDER BY created_at ASC",
            (user_id,),
        ).fetchall()
    return [r[0] for r in rows]


def save_memory(user_id: int, facts: list[str]) -> None:
    if not facts:
        return
    existing = set(load_memory(user_id))
    new_facts = [f for f in facts if f not in existing]
    if not new_facts:
        return
    with _conn() as con:
        con.executemany(
            "INSERT INTO user_memory (user_id, fact) VALUES (?, ?)",
            [(user_id, f) for f in new_facts],
        )
    logger.debug("user_id=%s | факты сохранены: %d", user_id, len(new_facts))


def clear_memory(user_id: int) -> None:
    with _conn() as con:
        con.execute("DELETE FROM user_memory WHERE user_id = ?", (user_id,))


# ── Напоминания ───────────────────────────────────────────────────────────────

def save_reminder(user_id: int, text: str, remind_at: datetime) -> int:
    with _conn() as con:
        cur = con.execute(
            "INSERT INTO reminders (user_id, text, remind_at) VALUES (?, ?, ?)",
            (user_id, text, remind_at.strftime(_TIME_FMT)),
        )
        return cur.lastrowid or 0


def get_due_reminders(now: datetime) -> list[tuple[int, int, str]]:
    """[(id, user_id, text)] — напоминания время которых пришло."""
    with _conn() as con:
        rows = con.execute("""
            SELECT id, user_id, text FROM reminders
            WHERE done = 0 AND remind_at <= ?
            ORDER BY remind_at ASC
        """, (now.strftime(_TIME_FMT),)).fetchall()
    return [(r[0], r[1], r[2]) for r in rows]


def mark_reminder_done(reminder_id: int) -> None:
    with _conn() as con:
        con.execute("UPDATE reminders SET done = 1 WHERE id = ?", (reminder_id,))


def get_active_reminders(user_id: int) -> list[tuple[int, str, str]]:
    """[(id, text, remind_at)] — активные напоминания пользователя."""
    with _conn() as con:
        rows = con.execute("""
            SELECT id, text, remind_at FROM reminders
            WHERE user_id = ? AND done = 0
            ORDER BY remind_at ASC
        """, (user_id,)).fetchall()
    return [(r[0], r[1], r[2]) for r in rows]


def delete_reminder(reminder_id: int, user_id: int) -> bool:
    with _conn() as con:
        cur = con.execute(
            "DELETE FROM reminders WHERE id = ? AND user_id = ?",
            (reminder_id, user_id),
        )
        return cur.rowcount > 0


# ── Дневник ───────────────────────────────────────────────────────────────────

def save_diary_entry(user_id: int, entry: str, mood: str = "") -> int:
    with _conn() as con:
        cur = con.execute(
            "INSERT INTO diary (user_id, entry, mood) VALUES (?, ?, ?)",
            (user_id, entry, mood),
        )
        return cur.lastrowid or 0


def load_diary_entries(user_id: int, limit: int = 5) -> list[tuple[str, str]]:
    """[(created_at, entry)] — последние записи дневника."""
    with _conn() as con:
        rows = con.execute(
            "SELECT created_at, entry FROM diary "
            "WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
    return [(r[0], r[1]) for r in rows]


# ── Задачи ────────────────────────────────────────────────────────────────────

def save_task(user_id: int, text: str, priority: int = 2, due_date: str = "") -> int:
    with _conn() as con:
        cur = con.execute(
            "INSERT INTO tasks (user_id, text, priority, due_date) VALUES (?, ?, ?, ?)",
            (user_id, text, priority, due_date),
        )
        return cur.lastrowid or 0


def get_active_tasks(user_id: int) -> list[tuple[int, str, int, str]]:
    """[(id, text, priority, due_date)] — незавершённые задачи по приоритету."""
    with _conn() as con:
        rows = con.execute("""
            SELECT id, text, priority, due_date FROM tasks
            WHERE user_id = ? AND done = 0
            ORDER BY priority ASC, created_at ASC
        """, (user_id,)).fetchall()
    return [(r[0], r[1], r[2], r[3]) for r in rows]


def mark_task_done(task_id: int, user_id: int) -> bool:
    with _conn() as con:
        cur = con.execute(
            "UPDATE tasks SET done = 1 WHERE id = ? AND user_id = ?",
            (task_id, user_id),
        )
        return cur.rowcount > 0


def delete_task(task_id: int, user_id: int) -> bool:
    with _conn() as con:
        cur = con.execute(
            "DELETE FROM tasks WHERE id = ? AND user_id = ?",
            (task_id, user_id),
        )
        return cur.rowcount > 0


# ── Настроение / эмоциональная память ────────────────────────────────────────

def save_mood(user_id: int, mood: str, context: str = "") -> None:
    with _conn() as con:
        con.execute(
            "INSERT INTO mood_log (user_id, mood, context) VALUES (?, ?, ?)",
            (user_id, mood, context),
        )


def get_recent_moods(user_id: int, limit: int = 7) -> list[tuple[str, str, str]]:
    """[(mood, context, created_at)] — последние N записей настроения."""
    with _conn() as con:
        rows = con.execute("""
            SELECT mood, context, created_at FROM mood_log
            WHERE user_id = ?
            ORDER BY created_at DESC LIMIT ?
        """, (user_id, limit)).fetchall()
    return [(r[0], r[1], r[2]) for r in rows]
