"""
database.py — работа с SQLite.
WAL-режим, connection-per-call через контекстный менеджер.
"""
import logging
import sqlite3
import calendar
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Generator

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

logger = logging.getLogger(__name__)

# Railway Volume — данные хранятся на постоянном диске
# Локально (разработка): ./database.db
# Railway (прод): /data/database.db (Volume примонтирован к /data)
import os as _os
DB_PATH = Path(_os.getenv("DB_PATH", "database.db"))
_TIME_FMT = "%Y-%m-%d %H:%M:%S"


def _migrate(con: sqlite3.Connection) -> None:
    """Идемпотентные ALTER TABLE миграции — запускаются один раз при старте."""
    existing = {row[1] for row in con.execute("PRAGMA table_info(reminders)").fetchall()}
    if "recurrence" not in existing:
        con.execute("ALTER TABLE reminders ADD COLUMN recurrence TEXT DEFAULT NULL")


@contextmanager
def _conn() -> Generator[sqlite3.Connection, None, None]:
    """
    Контекстный менеджер соединения с авто-commit/rollback.
    Retry при SQLITE_BUSY — до 5 попыток с backoff.
    """
    import time as _time
    last_err = None
    for attempt in range(5):
        try:
            con = sqlite3.connect(
                DB_PATH,
                detect_types=sqlite3.PARSE_DECLTYPES,
                timeout=10,        # ждём lock до 10 сек
            )
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("PRAGMA synchronous=NORMAL")
            con.execute("PRAGMA foreign_keys=ON")
            con.execute("PRAGMA busy_timeout=5000")  # ms
            try:
                yield con
                con.commit()
                return
            except sqlite3.OperationalError as e:
                con.rollback()
                raise
            except Exception:
                con.rollback()
                raise
            finally:
                con.close()
        except sqlite3.OperationalError as e:
            last_err = e
            if "locked" in str(e).lower() and attempt < 4:
                _time.sleep(0.1 * (2 ** attempt))  # 0.1, 0.2, 0.4, 0.8s
                continue
            raise
    raise last_err


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
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                text        TEXT    NOT NULL,
                remind_at   DATETIME NOT NULL,
                done        INTEGER  NOT NULL DEFAULT 0,
                recurrence  TEXT     DEFAULT NULL,  -- 'daily','weekly','weekday','monthly' или NULL
                created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            -- recurrence добавлена в схему — миграция через PRAGMA ниже
            CREATE TABLE IF NOT EXISTS diary (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER NOT NULL,
                entry      TEXT    NOT NULL,
                mood       TEXT    DEFAULT '',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS tasks (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER NOT NULL,
                text       TEXT    NOT NULL,
                priority   INTEGER NOT NULL DEFAULT 2,
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
            CREATE INDEX IF NOT EXISTS idx_history_user
                ON history(user_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_memory_user
                ON user_memory(user_id);
            CREATE INDEX IF NOT EXISTS idx_reminders_due
                ON reminders(remind_at, done);
            CREATE INDEX IF NOT EXISTS idx_diary_user
                ON diary(user_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_tasks_user
                ON tasks(user_id, done);
            CREATE INDEX IF NOT EXISTS idx_mood_user
                ON mood_log(user_id, created_at);
            CREATE TABLE IF NOT EXISTS structured_memory (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER NOT NULL,
                category   TEXT    NOT NULL,
                key        TEXT    NOT NULL,
                value      TEXT    NOT NULL,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, category, key) ON CONFLICT REPLACE
            );
            CREATE INDEX IF NOT EXISTS idx_structured_memory_user
                ON structured_memory(user_id, category);
            CREATE TABLE IF NOT EXISTS interaction_memory (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER NOT NULL,
                topic      TEXT    NOT NULL,
                summary    TEXT    NOT NULL,
                frequency  INTEGER DEFAULT 1,
                last_seen  DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_interaction_user
                ON interaction_memory(user_id, frequency DESC);
            CREATE TABLE IF NOT EXISTS events (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                date        TEXT NOT NULL,        -- YYYY-MM-DD
                time_start  TEXT,                 -- HH:MM или NULL (весь день)
                time_end    TEXT,                 -- HH:MM или NULL
                title       TEXT NOT NULL,
                description TEXT DEFAULT '',
                color       TEXT DEFAULT 'blue',  -- blue/green/red/orange/purple
                created_at  TEXT DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_events_date ON events(user_id, date);

            CREATE TABLE IF NOT EXISTS events (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                date        TEXT NOT NULL,
                time_start  TEXT,
                time_end    TEXT,
                title       TEXT NOT NULL,
                description TEXT DEFAULT '',
                color       TEXT DEFAULT 'blue'
            );
            CREATE INDEX IF NOT EXISTS idx_events_date ON events(user_id, date);

            CREATE TABLE IF NOT EXISTS conversation_context (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id      INTEGER NOT NULL UNIQUE,
                topic        TEXT    DEFAULT '',
                user_goal    TEXT    DEFAULT '',
                open_threads TEXT    DEFAULT '[]',
                last_summary TEXT    DEFAULT '',
                updated_at   DATETIME DEFAULT CURRENT_TIMESTAMP
            );
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


def clear_memory(user_id: int) -> None:
    with _conn() as con:
        con.execute("DELETE FROM user_memory WHERE user_id = ?", (user_id,))


# ── Напоминания ───────────────────────────────────────────────────────────────

def save_reminder(
    user_id: int,
    text: str,
    remind_at: datetime,
    recurrence: str | None = None,
) -> int:
    """Сохраняет напоминание. recurrence: 'daily','weekly','weekday','monthly' или None."""
    with _conn() as con:
        cur = con.execute(
            "INSERT INTO reminders (user_id, text, remind_at, recurrence) VALUES (?, ?, ?, ?)",
            (user_id, text, remind_at.strftime(_TIME_FMT), recurrence),
        )
        return cur.lastrowid or 0


# NOTE: unused — candidate for removal
def next_reminder_time(remind_at: datetime, recurrence: str) -> datetime | None:
    """Вычисляет следующее время для повторяющегося напоминания."""
    from datetime import timedelta
    r = recurrence.lower().strip()
    if r == "daily":
        return remind_at + timedelta(days=1)
    if r == "weekly":
        return remind_at + timedelta(weeks=1)
    if r == "monthly":
        # Следующий месяц (тот же день)
        month = remind_at.month % 12 + 1
        year  = remind_at.year + (1 if remind_at.month == 12 else 0)
        try:
            return remind_at.replace(year=year, month=month)
        except ValueError:
            # 31 января → 28/29 февраля
            import calendar
            last_day = calendar.monthrange(year, month)[1]
            return remind_at.replace(year=year, month=month, day=last_day)
    if r == "weekday":
        # Следующий будний день
        next_dt = remind_at + timedelta(days=1)
        while next_dt.weekday() >= 5:  # 5=сб, 6=вс
            next_dt += timedelta(days=1)
        return next_dt
    return None


def reschedule_reminder(reminder_id: int) -> bool:
    """
    Для повторяющегося напоминания: помечает текущее done=1,
    создаёт следующее. Возвращает True если пересоздано.
    """
    with _conn() as con:
        row = con.execute(
            "SELECT user_id, text, remind_at, recurrence FROM reminders WHERE id = ?",
            (reminder_id,),
        ).fetchone()

        if not row:
            return False

        user_id, text, remind_at_str, recurrence = row

        if not recurrence:
            return False  # одноразовое

        mark_reminder_done(reminder_id)

        remind_at = datetime.strptime(remind_at_str, _TIME_FMT)
        next_dt   = next_reminder_time(remind_at, recurrence)
        if not next_dt:
            return False

        con.execute(
            "INSERT INTO reminders (user_id, text, remind_at, recurrence) VALUES (?, ?, ?, ?)",
            (user_id, text, next_dt.strftime(_TIME_FMT), recurrence),
        )
        return True


def get_due_reminders(now: datetime) -> list[tuple[int, int, str, str | None]]:
    """[(id, user_id, text, recurrence)] — напоминания время которых пришло."""
    with _conn() as con:
        rows = con.execute("""
            SELECT id, user_id, text, recurrence FROM reminders
            WHERE done = 0 AND remind_at <= ?
            ORDER BY remind_at ASC
        """, (now.strftime(_TIME_FMT),)).fetchall()
    return [(r[0], r[1], r[2], r[3]) for r in rows]


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


# ── Структурированная память ──────────────────────────────────────────────────

# Категории памяти
MEMORY_CATEGORIES = {
    "facts":       "Факты (имя, город, возраст, профессия)",
    "interests":   "Интересы и увлечения",
    "projects":    "Проекты над которыми работает",
    "skills":      "Навыки и компетенции",
    "preferences": "Предпочтения (стиль общения, любимые темы, привычки)",
    "goals":       "Цели и планы",
    "context":     "Текущий контекст (над чем сейчас работает, что происходит)",
}


def upsert_memory(user_id: int, category: str, key: str, value: str) -> None:
    """Сохраняет или обновляет запись в структурированной памяти."""
    now = datetime.utcnow().strftime(_TIME_FMT)
    with _conn() as con:
        con.execute("""
            INSERT INTO structured_memory (user_id, category, key, value, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id, category, key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at
        """, (user_id, category, key, value, now))


def get_structured_memory(user_id: int) -> dict[str, dict[str, str]]:
    """
    Возвращает всю структурированную память пользователя.
    Формат: {category: {key: value}}
    """
    with _conn() as con:
        rows = con.execute("""
            SELECT category, key, value FROM structured_memory
            WHERE user_id = ?
            ORDER BY category, updated_at DESC
        """, (user_id,)).fetchall()

    result: dict[str, dict[str, str]] = {}
    for category, key, value in rows:
        if category not in result:
            result[category] = {}
        result[category][key] = value
    return result


def get_memory_by_category(user_id: int, category: str) -> dict[str, str]:
    """Возвращает память одной категории. Формат: {key: value}"""
    with _conn() as con:
        rows = con.execute("""
            SELECT key, value FROM structured_memory
            WHERE user_id = ? AND category = ?
            ORDER BY updated_at DESC
        """, (user_id, category)).fetchall()
    return {r[0]: r[1] for r in rows}


def delete_memory_entry(user_id: int, category: str, key: str) -> bool:
    """Удаляет конкретную запись памяти."""
    with _conn() as con:
        cur = con.execute("""
            DELETE FROM structured_memory
            WHERE user_id = ? AND category = ? AND key = ?
        """, (user_id, category, key))
        return cur.rowcount > 0


def clear_structured_memory(user_id: int) -> None:
    """Очищает всю структурированную память пользователя."""
    with _conn() as con:
        con.execute(
            "DELETE FROM structured_memory WHERE user_id = ?", (user_id,)
        )


def format_memory_for_prompt(user_id: int) -> str:
    """
    Форматирует структурированную память в текст для системного промпта.
    Возвращает пустую строку если памяти нет.
    """
    memory = get_structured_memory(user_id)
    if not memory:
        return ""

    labels = {
        "facts":       "О Сократе",
        "interests":   "Интересы",
        "projects":    "Проекты",
        "skills":      "Навыки",
        "preferences": "Предпочтения",
        "goals":       "Цели",
        "context":     "Сейчас работает над",
    }

    lines = ["📋 Что RaYa знает о Сократе:"]
    for category, entries in memory.items():
        if not entries:
            continue
        label = labels.get(category, category)
        items = "; ".join(f"{k}: {v}" for k, v in entries.items())
        lines.append(f"  {label}: {items}")

    return "\n".join(lines)


# ── Контекст разговора ────────────────────────────────────────────────────────

def get_conversation_context(user_id: int) -> dict:
    """
    Возвращает текущий контекст разговора.
    Формат: {topic, user_goal, open_threads: [], last_summary, updated_at}
    """
    with _conn() as con:
        row = con.execute("""
            SELECT topic, user_goal, open_threads, last_summary, updated_at
            FROM conversation_context WHERE user_id = ?
        """, (user_id,)).fetchone()

    if not row:
        return {
            "topic": "", "user_goal": "",
            "open_threads": [], "last_summary": "", "updated_at": "",
        }

    import json as _json
    try:
        threads = _json.loads(row[2]) if row[2] else []
    except Exception:
        threads = []

    return {
        "topic":        row[0] or "",
        "user_goal":    row[1] or "",
        "open_threads": threads,
        "last_summary": row[3] or "",
        "updated_at":   row[4] or "",
    }


def save_conversation_context(
    user_id: int,
    topic: str = "",
    user_goal: str = "",
    open_threads: list | None = None,
    last_summary: str = "",
) -> None:
    """Сохраняет или обновляет контекст разговора."""
    now     = datetime.utcnow().strftime(_TIME_FMT)
    threads = _json.dumps(open_threads or [], ensure_ascii=False)

    with _conn() as con:
        con.execute("""
            INSERT INTO conversation_context
                (user_id, topic, user_goal, open_threads, last_summary, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                topic        = excluded.topic,
                user_goal    = excluded.user_goal,
                open_threads = excluded.open_threads,
                last_summary = excluded.last_summary,
                updated_at   = excluded.updated_at
        """, (user_id, topic, user_goal, threads, last_summary, now))


def format_context_for_prompt(user_id: int) -> str:
    """Форматирует контекст разговора для системного промпта."""
    ctx = get_conversation_context(user_id)

    if not any([ctx["topic"], ctx["user_goal"],
                ctx["open_threads"], ctx["last_summary"]]):
        return ""

    lines = ["🗣️ Контекст текущего разговора:"]

    if ctx["topic"]:
        lines.append(f"  Тема: {ctx['topic']}")
    if ctx["user_goal"]:
        lines.append(f"  Цель Сократа: {ctx['user_goal']}")
    if ctx["open_threads"]:
        threads = "; ".join(ctx["open_threads"][:3])
        lines.append(f"  Незавершённые темы: {threads}")
    if ctx["last_summary"]:
        lines.append(f"  Что обсуждали: {ctx['last_summary']}")

    return "\n".join(lines)


# ── Память взаимодействий ─────────────────────────────────────────────────────

def upsert_interaction(user_id: int, topic: str, summary: str) -> None:
    """Добавляет или обновляет запись о теме разговора."""
    now = datetime.utcnow().strftime(_TIME_FMT)
    with _conn() as con:
        existing = con.execute("""
            SELECT id, frequency FROM interaction_memory
            WHERE user_id = ? AND topic = ?
        """, (user_id, topic)).fetchone()

        if existing:
            con.execute("""
                UPDATE interaction_memory
                SET frequency = frequency + 1, summary = ?, last_seen = ?
                WHERE id = ?
            """, (summary, now, existing[0]))
        else:
            con.execute("""
                INSERT INTO interaction_memory (user_id, topic, summary, last_seen)
                VALUES (?, ?, ?, ?)
            """, (user_id, topic, summary, now))


def get_top_interactions(user_id: int, limit: int = 5) -> list[tuple[str, str, int]]:
    """Возвращает топ тем по частоте: [(topic, summary, frequency)]."""
    with _conn() as con:
        rows = con.execute("""
            SELECT topic, summary, frequency FROM interaction_memory
            WHERE user_id = ?
            ORDER BY frequency DESC, last_seen DESC
            LIMIT ?
        """, (user_id, limit)).fetchall()
    return [(r[0], r[1], r[2]) for r in rows]


def format_interaction_memory(user_id: int) -> str:
    """Форматирует память взаимодействий для системного промпта."""
    rows = get_top_interactions(user_id, limit=4)
    if not rows:
        return ""

    lines = ["🔁 Темы которые Сократ поднимал раньше:"]
    for topic, summary, freq in rows:
        times = "несколько раз" if freq >= 3 else "уже обсуждали"
        lines.append(f"  • {topic} ({times}): {summary}")
    lines.append(
        "Если новое сообщение связано с этим — можешь сослаться на прошлый разговор."
    )
    return "\n".join(lines)

# ══════════════════════════════════════════════════════════
# CALENDAR EVENTS
# ══════════════════════════════════════════════════════════

def save_event(user_id: int, date: str, title: str,
               time_start: str = "", time_end: str = "",
               description: str = "", color: str = "blue") -> int:
    """Сохраняет событие. Возвращает id."""
    with _conn() as con:
        cur = con.execute(
            """INSERT INTO events (user_id, date, time_start, time_end, title, description, color)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (user_id, date, time_start or None, time_end or None, title, description, color)
        )
        return cur.lastrowid


def get_events_for_date(user_id: int, date: str) -> list[dict]:
    """Возвращает события на конкретный день, отсортированные по времени."""
    with _conn() as con:
        rows = con.execute(
            """SELECT id, date, time_start, time_end, title, description, color
               FROM events WHERE user_id = ? AND date = ?
               ORDER BY CASE WHEN time_start IS NULL THEN '99:99' ELSE time_start END""",
            (user_id, date)
        ).fetchall()
    return [{"id": r[0], "date": r[1], "time_start": r[2] or "",
             "time_end": r[3] or "", "title": r[4],
             "description": r[5], "color": r[6]} for r in rows]


def get_events_for_month(user_id: int, year: int, month: int) -> list[dict]:
    """Возвращает все события за месяц."""
    prefix = f"{year:04d}-{month:02d}"
    with _conn() as con:
        rows = con.execute(
            """SELECT id, date, time_start, time_end, title, description, color
               FROM events WHERE user_id = ? AND date LIKE ?
               ORDER BY date, CASE WHEN time_start IS NULL THEN '99:99' ELSE time_start END""",
            (user_id, f"{prefix}-%")
        ).fetchall()
    return [{"id": r[0], "date": r[1], "time_start": r[2] or "",
             "time_end": r[3] or "", "title": r[4],
             "description": r[5], "color": r[6]} for r in rows]


def update_event(event_id: int, user_id: int, **kwargs) -> bool:
    """Обновляет поля события."""
    allowed = {"date", "time_start", "time_end", "title", "description", "color"}
    fields  = {k: v for k, v in kwargs.items() if k in allowed}
    if not fields:
        return False
    sets = ", ".join(f"{k} = ?" for k in fields)
    vals = list(fields.values()) + [event_id, user_id]
    with _conn() as con:
        cur = con.execute(
            f"UPDATE events SET {sets} WHERE id = ? AND user_id = ?", vals
        )
        return cur.rowcount > 0


def delete_event(event_id: int, user_id: int) -> bool:
    """Удаляет событие."""
    with _conn() as con:
        cur = con.execute(
            "DELETE FROM events WHERE id = ? AND user_id = ?", (event_id, user_id)
        )
        return cur.rowcount > 0


def get_upcoming_events(user_id: int, limit: int = 5) -> list[dict]:
    """События от сегодня вперёд."""
    with _conn() as con:
        rows = con.execute(
            """SELECT id, date, time_start, time_end, title, description, color
               FROM events WHERE user_id = ? AND date >= date('now')
               ORDER BY date, CASE WHEN time_start IS NULL THEN '99:99' ELSE time_start END
               LIMIT ?""",
            (user_id, limit)
        ).fetchall()
    return [{"id": r[0], "date": r[1], "time_start": r[2] or "",
             "time_end": r[3] or "", "title": r[4],
             "description": r[5], "color": r[6]} for r in rows]

# ════════════════════════════════════════════════════════
# CALENDAR EVENTS
# ════════════════════════════════════════════════════════

def save_event(user_id: int, date: str, title: str,
               time_start: str = "", time_end: str = "",
               description: str = "", color: str = "blue") -> int:
    with _conn() as con:
        cur = con.execute(
            """INSERT INTO events
               (user_id, date, time_start, time_end, title, description, color)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (user_id, date, time_start or None, time_end or None,
             title, description, color)
        )
        return cur.lastrowid


def get_events_for_date(user_id: int, date: str) -> list[dict]:
    with _conn() as con:
        rows = con.execute(
            """SELECT id, date, time_start, time_end, title, description, color
               FROM events WHERE user_id=? AND date=?
               ORDER BY CASE WHEN time_start IS NULL THEN '99:99' ELSE time_start END""",
            (user_id, date)
        ).fetchall()
    return [_event_row(r) for r in rows]


def get_events_for_month(user_id: int, year: int, month: int) -> list[dict]:
    prefix = f"{year:04d}-{month:02d}"
    with _conn() as con:
        rows = con.execute(
            """SELECT id, date, time_start, time_end, title, description, color
               FROM events WHERE user_id=? AND date LIKE ?
               ORDER BY date,
               CASE WHEN time_start IS NULL THEN '99:99' ELSE time_start END""",
            (user_id, f"{prefix}-%")
        ).fetchall()
    return [_event_row(r) for r in rows]


def update_event(event_id: int, user_id: int, **kwargs) -> bool:
    allowed = {"date", "time_start", "time_end", "title", "description", "color"}
    fields  = {k: v for k, v in kwargs.items() if k in allowed}
    if not fields:
        return False
    sets = ", ".join(f"{k}=?" for k in fields)
    vals = list(fields.values()) + [event_id, user_id]
    with _conn() as con:
        cur = con.execute(
            f"UPDATE events SET {sets} WHERE id=? AND user_id=?", vals
        )
        return cur.rowcount > 0


def delete_event(event_id: int, user_id: int) -> bool:
    with _conn() as con:
        cur = con.execute(
            "DELETE FROM events WHERE id=? AND user_id=?", (event_id, user_id)
        )
        return cur.rowcount > 0


def get_upcoming_events(user_id: int, limit: int = 7) -> list[dict]:
    with _conn() as con:
        rows = con.execute(
            """SELECT id, date, time_start, time_end, title, description, color
               FROM events WHERE user_id=? AND date >= date('now')
               ORDER BY date,
               CASE WHEN time_start IS NULL THEN '99:99' ELSE time_start END
               LIMIT ?""",
            (user_id, limit)
        ).fetchall()
    return [_event_row(r) for r in rows]


def _event_row(r) -> dict:
    return {
        "id": r[0], "date": r[1],
        "time_start": r[2] or "", "time_end": r[3] or "",
        "title": r[4], "description": r[5], "color": r[6],
    }
