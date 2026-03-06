import sqlite3
import json
import logging
from pathlib import Path
from langchain_core.messages import HumanMessage, AIMessage, BaseMessage

logger = logging.getLogger(__name__)

DB_PATH = Path("database.db")


def init_db() -> None:
    """Создаёт таблицу если её нет."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS history (
                user_id  INTEGER NOT NULL,
                role     TEXT NOT NULL,
                content  TEXT NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()
    logger.info("✅ База данных инициализирована: %s", DB_PATH)


def load_history(user_id: int, limit: int = 20) -> list[BaseMessage]:
    """Загружает последние сообщения пользователя из БД."""
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute("""
            SELECT role, content FROM (
                SELECT role, content, created_at
                FROM history
                WHERE user_id = ?
                ORDER BY created_at DESC
                LIMIT ?
            ) ORDER BY created_at ASC
        """, (user_id, limit)).fetchall()

    messages: list[BaseMessage] = []
    for role, content in rows:
        if role == "human":
            messages.append(HumanMessage(content=content))
        elif role == "ai":
            messages.append(AIMessage(content=content))
    return messages


def save_messages(user_id: int, human: str, ai: str) -> None:
    """Сохраняет пару сообщений (вопрос + ответ) в БД."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.executemany(
            "INSERT INTO history (user_id, role, content) VALUES (?, ?, ?)",
            [(user_id, "human", human), (user_id, "ai", ai)]
        )
        conn.commit()


def clear_history(user_id: int) -> None:
    """Удаляет всю историю пользователя из БД."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM history WHERE user_id = ?", (user_id,))
        conn.commit()
