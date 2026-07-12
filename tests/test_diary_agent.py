"""
test_diary_agent.py — запись в дневник, чтение, извлечение настроения.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.agents.base_agent import AgentContext
from app.agents.diary_agent import DiaryAgent


@pytest.fixture
def agent():
    return DiaryAgent()


def _ctx(message: str) -> AgentContext:
    return AgentContext(user_id=1, message=message, user_name="tester")


def _with_response(agent: DiaryAgent, text: str) -> None:
    agent._llm = MagicMock(ainvoke=AsyncMock(return_value=MagicMock(content=text)))


async def test_write_entry_persists_with_mood(agent, temp_db):
    _with_response(
        agent,
        "Похоже, сегодня был продуктивный день! "
        "<diary_entry>Сегодня закончил важный проект на работе</diary_entry>"
        "<diary_mood>гордость</diary_mood>",
    )
    result = await agent._execute(_ctx("сегодня я закончил важный проект"))
    assert result.success is True
    assert "<diary_entry>" not in result.content
    assert result.metadata["mood"] == "гордость"

    entries = temp_db.load_diary_entries(1, limit=5)
    assert any("важный проект" in e[1] for e in entries)


async def test_invalid_mood_falls_back_to_neutral(agent, temp_db):
    _with_response(
        agent,
        "Записала! <diary_entry>Что-то случилось</diary_entry><diary_mood>несуществующее</diary_mood>",
    )
    result = await agent._execute(_ctx("сегодня было странно"))
    assert result.metadata["mood"] == "нейтрально"


async def test_missing_tags_uses_raw_message_as_entry(agent, temp_db):
    """Модель иногда забывает теги — запись всё равно должна сохраниться."""
    _with_response(agent, "Окей, поняла.")
    result = await agent._execute(_ctx("сегодня был обычный день"))
    assert result.success is True
    entries = temp_db.load_diary_entries(1, limit=5)
    assert any("обычный день" in e[1] for e in entries)


async def test_read_triggers_show_recent_entries_without_llm(agent, temp_db):
    temp_db.save_diary_entry(1, "Старая запись про отпуск", mood="радость")

    result = await agent._execute(_ctx("покажи дневник"))
    assert result.success is True
    assert "отпуск" in result.content
