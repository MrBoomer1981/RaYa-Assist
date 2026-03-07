import sqlite3
import logging
from pathlib import Path
from langchain_core.messages import HumanMessage, AIMessage, BaseMessage

logger = logging.getLogger(__name__)

DB_PATH = Path("database.db")


def init_db() -> None:
    """Создаёт таблицы и индексы если их нет."""
    with sqlite3.connect(DB_PATH) as conn:
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
        # Индексы для быстрой выборки по user_id
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_history_user
            ON history(user_id, created_at)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_memory_user
            ON user_memory(user_id)
        """)
        conn.commit()
    logger.info("✅ База данных готова: %s", DB_PATH)


# ── История разговора ──────────────────────────────────────────────────────────

def load_history(user_id: int, limit: int = 20) -> list[BaseMessage]:
    """Загружает последние N сообщений пользователя в хронологическом порядке."""
    with sqlite3.connect(DB_PATH) as conn:
        # Алиас sub обязателен для совместимости со всеми версиями SQLite
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

    result: list[BaseMessage] = []
    for role, content in rows:
        if role == "human":
            result.append(HumanMessage(content=content))
        elif role == "ai":
            result.append(AIMessage(content=content))
    return result


def save_messages(user_id: int, human: str, ai: str) -> None:
    """Сохраняет пару сообщений (вопрос + ответ) одной транзакцией."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.executemany(
            "INSERT INTO history (user_id, role, content) VALUES (?, ?, ?)",
            [(user_id, "human", human), (user_id, "ai", ai)],
        )
        conn.commit()


def clear_history(user_id: int) -> None:
    """Удаляет историю разговора пользователя."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM history WHERE user_id = ?", (user_id,))
        conn.commit()


# ── Долгосрочная память ────────────────────────────────────────────────────────

def load_memory(user_id: int) -> list[str]:
    """Возвращает все сохранённые факты о пользователе."""
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT fact FROM user_memory WHERE user_id = ? ORDER BY created_at ASC",
            (user_id,),
        ).fetchall()
    return [row[0] for row in rows]


def save_memory(user_id: int, facts: list[str]) -> None:
    """Сохраняет новые факты, пропуская дубли."""
    if not facts:
        return
    existing = set(load_memory(user_id))
    new_facts = [f for f in facts if f not in existing]
    if not new_facts:
        return
    with sqlite3.connect(DB_PATH) as conn:
        conn.executemany(
            "INSERT INTO user_memory (user_id, fact) VALUES (?, ?)",
            [(user_id, fact) for fact in new_facts],
        )
        conn.commit()
    logger.debug("user_id=%s | сохранено фактов: %d", user_id, len(new_facts))


def clear_memory(user_id: int) -> None:
    """Удаляет всю память о пользователе."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM user_memory WHERE user_id = ?", (user_id,))
        conn.commit()
