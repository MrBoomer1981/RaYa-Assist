"""
test_diary_agent.py — запись в дневник, чтение, извлечение настроения.
"""
from datetime import datetime
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


# ── Временная метка записи — по МСК, а не UTC ────────────────────────────────
# Регрессия: now_str в diary_agent.py считался через utcnow() напрямую.
# В окне UTC 21:00-23:59 (МСК уже следующие сутки) запись, сделанная
# "сегодня" по ощущению пользователя, помечалась вчерашней UTC-датой.

async def test_entry_timestamp_uses_msk_near_midnight_rollover(agent, temp_db, monkeypatch):
    import app.agents.diary_agent as diary_mod

    fake_now_msk = datetime(2026, 7, 15, 1, 30)  # среда, 01:30 МСК (было 22:30 UTC вторника)
    monkeypatch.setattr(diary_mod, "now_msk", lambda: fake_now_msk)

    _with_response(agent, "Записала! <diary_entry>Поздняя запись перед сном</diary_entry>")
    result = await agent._execute(_ctx("записал мысль перед сном"))
    assert result.success is True

    entries = temp_db.load_diary_entries(1, limit=5)
    entry_text = next(e[1] for e in entries if "Поздняя запись" in e[1])
    assert "2026-07-15" in entry_text
    assert "2026-07-14" not in entry_text
