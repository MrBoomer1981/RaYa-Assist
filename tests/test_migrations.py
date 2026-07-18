"""
test_migrations.py — версионированные миграции БД.

Самое важное здесь — обратная совместимость: у уже существующих (в проде)
баз колонки могли появиться СТАРЫМ способом ДО перехода на эту систему,
то есть PRAGMA user_version у них будет 0, но сами колонки уже на месте.
Если бы миграции не были идемпотентны, апгрейд такой базы упал бы на
"duplicate column name".
"""
import sqlite3

import pytest

from app.migrations import MIGRATIONS, run_migrations


@pytest.fixture
def raw_con(tmp_path):
    """"Голое" sqlite-соединение с нуля — без init_db(), чтобы контролировать схему вручную."""
    db_file = tmp_path / "raw.db"
    con = sqlite3.connect(str(db_file))
    yield con
    con.close()


def _minimal_schema(con: sqlite3.Connection) -> None:
    """Минимальная схема — как будто это очень старая версия бота до всех миграций."""
    con.executescript("""
        CREATE TABLE reminders (
            id INTEGER PRIMARY KEY, user_id INTEGER, text TEXT,
            remind_at DATETIME, done INTEGER DEFAULT 0
        );
        CREATE TABLE events (
            id INTEGER PRIMARY KEY, user_id INTEGER, date TEXT,
            time_start TEXT, time_end TEXT, title TEXT, description TEXT, color TEXT
        );
        CREATE TABLE tasks (
            id INTEGER PRIMARY KEY, user_id INTEGER, text TEXT,
            priority INTEGER DEFAULT 2, due_date TEXT, done INTEGER DEFAULT 0,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE users (
            user_id INTEGER PRIMARY KEY, first_name TEXT
        );
        CREATE TABLE knowledge_cache (
            id INTEGER PRIMARY KEY, query_hash TEXT, mode TEXT, result TEXT, expires_at REAL
        );
    """)
    con.commit()


def test_fresh_minimal_schema_reaches_latest_version(raw_con):
    _minimal_schema(raw_con)
    run_migrations(raw_con)

    version = raw_con.execute("PRAGMA user_version").fetchone()[0]
    assert version == MIGRATIONS[-1][0]


def test_all_target_columns_exist_after_migration(raw_con):
    _minimal_schema(raw_con)
    run_migrations(raw_con)

    def cols(table):
        return {row[1] for row in raw_con.execute(f"PRAGMA table_info({table})").fetchall()}

    assert "recurrence" in cols("reminders")
    assert {"importance", "repeat", "remind_days"}.issubset(cols("events"))
    assert {"last_name", "username", "updated_at"}.issubset(cols("users"))
    assert "digest_subscribed" in cols("users")
    assert "query" in cols("knowledge_cache")


def test_running_twice_is_safe_and_idempotent(raw_con):
    _minimal_schema(raw_con)
    run_migrations(raw_con)
    run_migrations(raw_con)  # не должно кидать "duplicate column"

    version = raw_con.execute("PRAGMA user_version").fetchone()[0]
    assert version == MIGRATIONS[-1][0]


def test_backward_compat_columns_already_present_with_zero_version(raw_con):
    """
    КРИТИЧНЫЙ сценарий: база уже получила все колонки СТАРЫМ способом
    (до перехода на версионирование), user_version при этом всё ещё 0.
    Раньше это привело бы к 'duplicate column name' при первом же ALTER TABLE.
    """
    _minimal_schema(raw_con)
    # Вручную добавляем колонки — как будто их добавил старый ad-hoc код
    raw_con.executescript("""
        ALTER TABLE reminders ADD COLUMN recurrence TEXT DEFAULT NULL;
        ALTER TABLE events ADD COLUMN importance INTEGER DEFAULT 0;
        ALTER TABLE events ADD COLUMN repeat TEXT DEFAULT '';
        ALTER TABLE events ADD COLUMN remind_days INTEGER DEFAULT 0;
        ALTER TABLE users ADD COLUMN last_name TEXT DEFAULT '';
        ALTER TABLE users ADD COLUMN username TEXT DEFAULT '';
        ALTER TABLE users ADD COLUMN updated_at TEXT DEFAULT '';
        ALTER TABLE knowledge_cache ADD COLUMN query TEXT NOT NULL DEFAULT '';
    """)
    raw_con.commit()
    assert raw_con.execute("PRAGMA user_version").fetchone()[0] == 0  # ещё не версионировано

    run_migrations(raw_con)  # не должно упасть

    version = raw_con.execute("PRAGMA user_version").fetchone()[0]
    assert version == MIGRATIONS[-1][0]


def test_partial_migration_state_applies_only_missing(raw_con):
    """Часть колонок уже есть, часть — нет; version=0. Должны добавиться только недостающие."""
    _minimal_schema(raw_con)
    raw_con.execute("ALTER TABLE reminders ADD COLUMN recurrence TEXT DEFAULT NULL")
    raw_con.commit()

    run_migrations(raw_con)  # events/users/knowledge_cache всё ещё нужно мигрировать

    cols = {row[1] for row in raw_con.execute("PRAGMA table_info(events)").fetchall()}
    assert "importance" in cols


def test_tasks_migration_preserves_data_from_old_column_name(raw_con):
    """tasks: старая схема с произвольным именем текстовой колонки — данные не теряются."""
    raw_con.executescript("""
        CREATE TABLE tasks (
            id INTEGER PRIMARY KEY, user_id INTEGER, task_text TEXT,
            priority INTEGER DEFAULT 2, done INTEGER DEFAULT 0
        );
        CREATE TABLE reminders (id INTEGER PRIMARY KEY, user_id INTEGER, text TEXT, remind_at DATETIME, done INTEGER DEFAULT 0);
        CREATE TABLE events (id INTEGER PRIMARY KEY, user_id INTEGER, date TEXT, title TEXT);
        CREATE TABLE users (user_id INTEGER PRIMARY KEY, first_name TEXT);
        CREATE TABLE knowledge_cache (id INTEGER PRIMARY KEY, query_hash TEXT, mode TEXT, result TEXT, expires_at REAL);
        INSERT INTO tasks (user_id, task_text, priority, done) VALUES (1, 'Старая задача', 1, 0);
    """)
    raw_con.commit()

    run_migrations(raw_con)

    row = raw_con.execute("SELECT text, priority FROM tasks WHERE user_id=1").fetchone()
    assert row == ("Старая задача", 1)


def test_no_migration_needed_skips_when_version_current(raw_con, monkeypatch):
    """Оптимизация: если user_version уже на уровне последней миграции — сами миграции не запускаются."""
    import app.migrations as migrations_module

    was_called = False
    def _tracking(con):
        nonlocal was_called
        was_called = True
        return False

    monkeypatch.setattr(migrations_module, "MIGRATIONS", [(1, "тестовая миграция", _tracking)])
    raw_con.execute("PRAGMA user_version = 1")  # уже "на последней" версии по меркам патченного списка

    run_migrations(raw_con)
    assert was_called is False


# ── Миграция 006: подписка на дайджест ──────────────────────────────────────
# Регрессия на баг: раньше дайджест уходил единственному угаданному
# получателю (known_users[0], см. app/proactive_service.py). Теперь это
# рассылка по подписчикам (users.digest_subscribed), а владельца
# (OWNER_USER_ID, если он уже задан) миграция подписывает автоматически,
# чтобы поведение не изменилось для уже работающих деплоев.

def test_owner_auto_subscribed_to_digest_if_configured(raw_con, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "owner_user_id", 42)

    _minimal_schema(raw_con)
    raw_con.execute("INSERT INTO users (user_id, first_name) VALUES (42, 'Виктор')")
    raw_con.commit()

    run_migrations(raw_con)

    row = raw_con.execute("SELECT digest_subscribed FROM users WHERE user_id = 42").fetchone()
    assert row[0] == 1


def test_owner_row_created_by_migration_if_never_messaged_bot(raw_con, monkeypatch):
    """OWNER_USER_ID задан, но этот user_id ещё ни разу не писал боту — строки в users нет."""
    from app.config import settings
    monkeypatch.setattr(settings, "owner_user_id", 999)

    _minimal_schema(raw_con)  # строки для user_id=999 нет

    run_migrations(raw_con)

    row = raw_con.execute("SELECT digest_subscribed FROM users WHERE user_id = 999").fetchone()
    assert row is not None and row[0] == 1


def test_no_owner_configured_nobody_auto_subscribed(raw_con, monkeypatch):
    """owner_user_id = 0 (dev-режим, не настроено) — никого не подписываем автоматически."""
    from app.config import settings
    monkeypatch.setattr(settings, "owner_user_id", 0)

    _minimal_schema(raw_con)
    raw_con.execute("INSERT INTO users (user_id, first_name) VALUES (42, 'Виктор')")
    raw_con.commit()

    run_migrations(raw_con)

    row = raw_con.execute("SELECT digest_subscribed FROM users WHERE user_id = 42").fetchone()
    assert row[0] == 0
