"""
diary_agent.py — агент личного дневника.
Принимает записи, помогает с рефлексией, анализирует паттерны.
Данные хранятся в отдельной таблице — приватны и изолированы.
"""
import logging
import sqlite3
from datetime import datetime
from pathlib import Path

from app.agents.base_agent import AgentContext, AgentResult, BaseAgent
from app.database import DB_PATH

logger = logging.getLogger(__name__)

_SYSTEM = """\
Ты хранитель личного дневника Сократа.

Твои задачи:
- Принимать и сохранять личные записи
- Помогать с рефлексией и осмыслением событий
- Замечать паттерны в настроении и мыслях
- Задавать глубокие вопросы которые помогают думать
- Поддерживать без лишних советов — если Сократ хочет просто выговориться

Тон: тёплый, внимательный, без осуждения.
Обращайся только "Сократ". Никогда не делись записями с другими агентами."""


def _ensure_diary_table() -> None:
    """Создаёт таблицу дневника если не существует."""
    conn = sqlite3.connect(str(DB_PATH))
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS diary (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER NOT NULL,
                entry      TEXT    NOT NULL,
                mood       TEXT    DEFAULT '',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_diary_user
            ON diary(user_id, created_at)
        """)
        conn.commit()
    finally:
        conn.close()


def _save_entry(user_id: int, entry: str, mood: str = "") -> int:
    """Сохраняет запись в дневник. Возвращает id."""
    conn = sqlite3.connect(str(DB_PATH))
    try:
        cur = conn.execute(
            "INSERT INTO diary (user_id, entry, mood) VALUES (?, ?, ?)",
            (user_id, entry, mood),
        )
        conn.commit()
        return cur.lastrowid or 0
    finally:
        conn.close()


def _load_recent_entries(user_id: int, limit: int = 5) -> list[tuple[str, str]]:
    """Загружает последние записи. Возвращает [(created_at, entry)]."""
    conn = sqlite3.connect(str(DB_PATH))
    try:
        rows = conn.execute(
            "SELECT created_at, entry FROM diary "
            "WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
    finally:
        conn.close()
    return [(row[0], row[1]) for row in rows]


class DiaryAgent(BaseAgent):
    agent_name = "diary"
    timeout = 30

    def __init__(self) -> None:
        super().__init__()
        _ensure_diary_table()

    def _system_prompt(self) -> str:
        return _SYSTEM

    async def _execute(self, ctx: AgentContext) -> AgentResult:
        # Загружаем последние записи как контекст
        recent = _load_recent_entries(ctx.user_id, limit=5)
        context_str = ""
        if recent:
            lines = [f"[{dt}]: {entry[:200]}" for dt, entry in recent]
            context_str = (
                "\n\nПоследние записи в дневнике:\n" + "\n".join(lines)
            )

        user_content = ctx.message + context_str
        messages = self._build_messages(ctx, user_content=user_content)
        response = await self._llm.ainvoke(messages)
        reply = str(response.content)

        # Сохраняем запись (только если это не вопрос о дневнике)
        entry_id = 0
        if _is_new_entry(ctx.message):
            entry_id = _save_entry(ctx.user_id, ctx.message)
            logger.info("📔 Запись #%d сохранена в дневник user_id=%s", entry_id, ctx.user_id)

        return AgentResult(
            success=True,
            content=reply,
            agent_name=self.agent_name,
            needs_critic=False,
            metadata={"entry_saved": entry_id > 0, "entry_id": entry_id},
        )


def _is_new_entry(message: str) -> bool:
    """Определяет — это новая запись или вопрос о дневнике."""
    question_keywords = ("покажи", "прочитай", "что я писал", "найди", "когда я")
    msg_lower = message.lower()
    return not any(kw in msg_lower for kw in question_keywords)
