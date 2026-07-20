"""
Conversation memory service.
Stores per-user message history in SQLite.
Provides context window for LLM calls.
"""
import sqlite3
import time
from contextlib import contextmanager
from typing import List, Dict

from deeper.utils.logger import get_logger

logger = get_logger("memory")

# How many messages to keep in context window
MAX_HISTORY = 20
# How many to pass to LLM (last N)
CONTEXT_WINDOW = 12


class MemoryService:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._init_db()

    @contextmanager
    def _connect(self):
        """
        Раньше возвращала голый sqlite3.Connection, используемый как
        `with self._connect() as conn:` — контекстный менеджер
        sqlite3.Connection управляет только commit/rollback, но НЕ
        закрывает соединение (см. тот же фикс в knowledge_base.py и
        cache_manager.py — везде была одна и та же копипаста). Теперь
        настоящий контекстный менеджер: коммитит/откатывает и всегда закрывает.
        """
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS conversation_history (
                    id        INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id   INTEGER NOT NULL,
                    role      TEXT NOT NULL,
                    content   TEXT NOT NULL,
                    timestamp REAL NOT NULL
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_memory_user
                ON conversation_history(user_id, timestamp)
            """)
            conn.commit()
        logger.info("Memory DB initialized")

    def add_message(self, user_id: int, role: str, content: str) -> None:
        """Save a message to user's history. Trims to MAX_HISTORY."""
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO conversation_history (user_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
                (user_id, role, content[:4000], time.time()),
            )
            conn.commit()
        self._trim(user_id)

    def _trim(self, user_id: int) -> None:
        """Keep only last MAX_HISTORY messages per user."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id FROM conversation_history WHERE user_id = ? ORDER BY timestamp DESC",
                (user_id,)
            ).fetchall()
            if len(rows) > MAX_HISTORY:
                ids_to_delete = [r["id"] for r in rows[MAX_HISTORY:]]
                conn.execute(
                    f"DELETE FROM conversation_history WHERE id IN ({','.join('?' * len(ids_to_delete))})",
                    ids_to_delete,
                )
                conn.commit()

    def get_context(self, user_id: int) -> List[Dict[str, str]]:
        """Return last CONTEXT_WINDOW messages as list of {role, content} dicts."""
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT role, content FROM conversation_history
                   WHERE user_id = ?
                   ORDER BY timestamp DESC
                   LIMIT ?""",
                (user_id, CONTEXT_WINDOW),
            ).fetchall()
        # Reverse to chronological order
        messages = [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]
        return messages

    def clear(self, user_id: int) -> None:
        """Clear conversation history for a user."""
        with self._connect() as conn:
            conn.execute("DELETE FROM conversation_history WHERE user_id = ?", (user_id,))
            conn.commit()
        logger.info("Cleared memory for user {}", user_id)

    def count(self, user_id: int) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) as n FROM conversation_history WHERE user_id = ?",
                (user_id,)
            ).fetchone()
        return row["n"]
