"""
migrations.py — версионированные миграции схемы БД.

Раньше проверки "добавить колонку если её нет" были разбросаны по database.py
в двух разных стилях (PRAGMA table_info + if, и отдельный try/except-хак для
knowledge_cache.query). Здесь всё сведено в один пронумерованный список.

Дизайн:
- Каждая миграция — идемпотентная функция (сама проверяет своё условие).
  Это обязательно: у уже развёрнутых баз колонки могли появиться СТАРЫМ
  способом, и PRAGMA user_version для них будет 0 — версия сама по себе
  не гарантирует, что колонки физически отсутствуют.
- PRAGMA user_version используется как оптимизация: если база уже на
  последней версии, весь проход пропускается одним PRAGMA-запросом вместо
  N вызовов PRAGMA table_info() на каждый старт бота.

Чтобы добавить новую миграцию:
1. Написать идемпотентную функцию (con) -> bool (True если реально что-то изменила)
2. Добавить кортеж (номер, описание, функция) в конец MIGRATIONS
3. Номер должен быть на 1 больше предыдущего максимума
"""
import logging
import sqlite3
from typing import Callable

logger = logging.getLogger(__name__)


def _cols(con: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in con.execute(f"PRAGMA table_info({table})").fetchall()}


def _migration_001_reminders_recurrence(con: sqlite3.Connection) -> bool:
    """reminders: колонка recurrence для повторяющихся напоминаний."""
    if "recurrence" in _cols(con, "reminders"):
        return False
    con.execute("ALTER TABLE reminders ADD COLUMN recurrence TEXT DEFAULT NULL")
    return True


def _migration_002_events_importance_repeat_remind(con: sqlite3.Connection) -> bool:
    """events: importance (важность), repeat (повтор), remind_days (напоминание заранее)."""
    changed = False
    cols = _cols(con, "events")
    if "importance" not in cols:
        con.execute("ALTER TABLE events ADD COLUMN importance INTEGER DEFAULT 0")
        changed = True
    if "repeat" not in cols:
        con.execute("ALTER TABLE events ADD COLUMN repeat TEXT DEFAULT ''")
        changed = True
    if "remind_days" not in cols:
        con.execute("ALTER TABLE events ADD COLUMN remind_days INTEGER DEFAULT 0")
        changed = True
    return changed


def _migration_003_tasks_rename_text_column(con: sqlite3.Connection) -> bool:
    """tasks: переносит данные из старой схемы (произвольное имя текстовой колонки) в text."""
    cols = _cols(con, "tasks")
    if not cols or "text" in cols:
        return False

    system_cols = {"id", "user_id", "priority", "due_date", "done", "created_at", "text"}
    old_col  = next((c for c in cols if c not in system_cols), None)
    src_text = old_col if old_col else "''"
    src_prio = "priority"   if "priority"   in cols else "2"
    src_due  = "due_date"   if "due_date"   in cols else "''"
    src_done = "done"       if "done"       in cols else "0"
    src_ts   = "created_at" if "created_at" in cols else "CURRENT_TIMESTAMP"

    logger.info("Migration 003: tasks.%s -> tasks.text (текущие колонки: %s)", src_text, cols)
    con.executescript(f"""
        CREATE TABLE IF NOT EXISTS tasks_new (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER NOT NULL,
            text       TEXT    NOT NULL DEFAULT '',
            priority   INTEGER NOT NULL DEFAULT 2,
            due_date   TEXT    DEFAULT '',
            done       INTEGER NOT NULL DEFAULT 0,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        INSERT OR IGNORE INTO tasks_new (id, user_id, text, priority, due_date, done, created_at)
            SELECT id, user_id, {src_text}, {src_prio}, {src_due}, {src_done}, {src_ts} FROM tasks;
        DROP TABLE tasks;
        ALTER TABLE tasks_new RENAME TO tasks;
        CREATE INDEX IF NOT EXISTS idx_tasks_user ON tasks(user_id, done);
    """)
    return True


def _migration_004_users_missing_columns(con: sqlite3.Connection) -> bool:
    """users: last_name/username/updated_at для баз, созданных до этих колонок."""
    changed = False
    cols = _cols(con, "users")
    # CURRENT_TIMESTAMP нельзя использовать как DEFAULT в ALTER TABLE в SQLite
    for col, typedef in [
        ("last_name",  "TEXT DEFAULT ''"),
        ("username",   "TEXT DEFAULT ''"),
        ("updated_at", "TEXT DEFAULT ''"),
    ]:
        if col not in cols:
            con.execute(f"ALTER TABLE users ADD COLUMN {col} {typedef}")
            changed = True
    return changed


def _migration_005_knowledge_cache_query_column(con: sqlite3.Connection) -> bool:
    """knowledge_cache: колонка query (раньше добавлялась отдельным try/except-хаком)."""
    if "query" in _cols(con, "knowledge_cache"):
        return False
    con.execute("ALTER TABLE knowledge_cache ADD COLUMN query TEXT NOT NULL DEFAULT ''")
    return True


def _migration_006_users_digest_subscribed(con: sqlite3.Connection) -> bool:
    """
    users: digest_subscribed — явная подписка на утренний дайджест.

    Раньше дайджест уходил единственному "владельцу", которого при
    незаданном OWNER_USER_ID код угадывал как known_users[0] (минимальный
    user_id из истории) — из-за этого дайджест однажды ушёл случайному
    пользователю, заблокировавшему бота, а не владельцу. Теперь дайджест —
    рассылка по явным подписчикам (команда /digest), а не угадывание.

    Владельца (OWNER_USER_ID, если он уже задан на момент миграции)
    подписываем один раз автоматически — чтобы поведение не изменилось
    для уже работающих деплоев без ручных действий с их стороны.
    """
    changed = False
    if "digest_subscribed" not in _cols(con, "users"):
        con.execute("ALTER TABLE users ADD COLUMN digest_subscribed INTEGER NOT NULL DEFAULT 0")
        changed = True

    from app.config import settings  # локальный импорт — не тянуть app.config во все миграции
    if settings.owner_user_id:
        con.execute(
            """
            INSERT INTO users (user_id, digest_subscribed, updated_at)
            VALUES (?, 1, CURRENT_TIMESTAMP)
            ON CONFLICT(user_id) DO UPDATE SET digest_subscribed = 1
            """,
            (settings.owner_user_id,),
        )
        changed = True

    return changed


# Порядок важен: применяются строго по возрастанию номера.
MIGRATIONS: list[tuple[int, str, Callable[[sqlite3.Connection], bool]]] = [
    (1, "reminders.recurrence",                  _migration_001_reminders_recurrence),
    (2, "events.importance/repeat/remind_days",  _migration_002_events_importance_repeat_remind),
    (3, "tasks: перенос старой текстовой колонки в text", _migration_003_tasks_rename_text_column),
    (4, "users.last_name/username/updated_at",   _migration_004_users_missing_columns),
    (5, "knowledge_cache.query",                 _migration_005_knowledge_cache_query_column),
    (6, "users.digest_subscribed (+ автоподписка owner)", _migration_006_users_digest_subscribed),
]


def run_migrations(con: sqlite3.Connection) -> None:
    """
    Применяет все миграции с номером выше текущего PRAGMA user_version.

    Каждая миграция идемпотентна сама по себе — безопасно и для баз, уже
    получивших эти колонки старым, неверсионированным способом (у них
    user_version будет 0, но ALTER TABLE просто не найдёт что добавлять).
    """
    current = con.execute("PRAGMA user_version").fetchone()[0]
    latest = MIGRATIONS[-1][0] if MIGRATIONS else 0
    if current >= latest:
        return  # уже всё применено — не трогаем PRAGMA table_info() зря

    applied = 0
    for version, description, fn in MIGRATIONS:
        if version <= current:
            continue
        if fn(con):
            applied += 1
            logger.info("✅ Миграция %d применена: %s", version, description)

    con.execute(f"PRAGMA user_version = {latest}")
    con.commit()
    if applied:
        logger.info("✅ БД мигрирована до версии %d (%d изменений применено)", latest, applied)
