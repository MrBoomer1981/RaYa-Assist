import logging
import sqlite3
from pathlib import Path

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

logger = logging.getLogger(__name__)

DB_PATH = Path("database.db")


def init_db() -> None:
    """Создаёт таблицы, индексы и настраивает параметры БД."""
    conn = sqlite3.connect(DB_PATH)
    try:
        # Прагмы применяем один раз при инициализации
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS history (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER NOT NULL,
                role       TEXT    NOT NULL CHECK(role IN ('human', 'ai')),
                content    TEXT    NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_memory (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER NOT NULL,
                fact       TEXT    NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_history_user
            ON history(user_id, created_at)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_memory_user
            ON user_memory(user_id)
        """)
        conn.commit()
    finally:
        conn.close()
    logger.info("✅ База данных готова: %s", DB_PATH)


def _connect() -> sqlite3.Connection:
    """Открывает соединение с нужными прагмами."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# ── История разговора ──────────────────────────────────────────────────────────

def load_history(user_id: int, limit: int = 20) -> list[BaseMessage]:
    """Загружает последние N сообщений в хронологическом порядке."""
    conn = _connect()
    try:
        rows = conn.execute("""
            SELECT role, content
            FROM (
                SELECT role, content, created_at
                FROM history
                WHERE user_id = ?
                ORDER BY created_at DESC
                LIMIT ?
            ) AS sub
            ORDER BY created_at ASC
        """, (user_id, limit)).fetchall()
    finally:
        conn.close()

    result: list[BaseMessage] = []
    for role, content in rows:
        if role == "human":
            result.append(HumanMessage(content=content))
        elif role == "ai":
            result.append(AIMessage(content=content))
    return result


def save_messages(user_id: int, human: str, ai: str) -> None:
    """Сохраняет пару сообщений одной транзакцией."""
    conn = _connect()
    try:
        conn.executemany(
            "INSERT INTO history (user_id, role, content) VALUES (?, ?, ?)",
            [(user_id, "human", human), (user_id, "ai", ai)],
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def clear_history(user_id: int) -> None:
    """Удаляет историю разговора пользователя."""
    conn = _connect()
    try:
        conn.execute("DELETE FROM history WHERE user_id = ?", (user_id,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── Долгосрочная память ────────────────────────────────────────────────────────

def load_memory(user_id: int) -> list[str]:
    """Возвращает все сохранённые факты о пользователе."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT fact FROM user_memory WHERE user_id = ? ORDER BY created_at ASC",
            (user_id,),
        ).fetchall()
    finally:
        conn.close()
    return [row[0] for row in rows]


def save_memory(user_id: int, facts: list[str]) -> None:
    """Сохраняет новые факты, пропуская точные дубли."""
    if not facts:
        return
    existing = set(load_memory(user_id))
    new_facts = [f for f in facts if f not in existing]
    if not new_facts:
        return
    conn = _connect()
    try:
        conn.executemany(
            "INSERT INTO user_memory (user_id, fact) VALUES (?, ?)",
            [(user_id, fact) for fact in new_facts],
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    logger.debug("user_id=%s | сохранено фактов: %d", user_id, len(new_facts))


def clear_memory(user_id: int) -> None:
    """Удаляет всю память о пользователе."""
    conn = _connect()
    try:
        conn.execute("DELETE FROM user_memory WHERE user_id = ?", (user_id,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
