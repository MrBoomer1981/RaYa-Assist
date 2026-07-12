"""
test_morning_agent.py — сборка утреннего дайджеста.

Регрессия: раньше `_execute()` ссылался на переменную `photo_sched`, которая
нигде не присваивалась (баг после недописанной интеграции с фото-расписанием) —
NameError вылетал у КАЖДОГО пользователя, у которого не был настроен Obsidian
(то есть у всех). Тесты явно гоняют _execute() с разными комбинациями пустых
данных, чтобы такая регрессия ловилась немедленно.
"""
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest

import app.agents.morning_agent as morning_agent
from app.agents.base_agent import AgentContext
from app.agents.morning_agent import MorningAgent


@pytest.fixture
def agent():
    return MorningAgent()


def _ctx() -> AgentContext:
    return AgentContext(user_id=1, message="доброе утро", user_name="tester")


def _patch_sources(monkeypatch, weather="", events="", tasks="", news="", photo=""):
    monkeypatch.setattr(morning_agent, "_get_weather", AsyncMock(return_value=weather))
    monkeypatch.setattr(morning_agent, "_get_events", AsyncMock(return_value=events))
    monkeypatch.setattr(morning_agent, "_get_tasks", AsyncMock(return_value=tasks))
    monkeypatch.setattr(morning_agent, "_get_news_digest", AsyncMock(return_value=news))
    monkeypatch.setattr(morning_agent, "_get_photo_schedule", AsyncMock(return_value=photo))


async def test_all_sources_empty_does_not_crash(agent, monkeypatch):
    """Свежий пользователь: нет фото-расписания, нет событий, нет задач — не должно падать."""
    _patch_sources(monkeypatch)
    result = await agent._execute(_ctx())
    assert result.success is True
    assert "дайджест" in result.content.lower() or "утро" in result.content.lower()


async def test_photo_schedule_takes_priority_over_events(agent, monkeypatch):
    _patch_sources(monkeypatch, events="Событие из календаря", photo="Расписание с фото")
    result = await agent._execute(_ctx())
    assert "Расписание с фото" in result.content
    assert "Событие из календаря" not in result.content


async def test_falls_back_to_calendar_events_when_no_photo(agent, monkeypatch):
    _patch_sources(monkeypatch, events="Встреча в 10:00", photo="")
    result = await agent._execute(_ctx())
    assert "Встреча в 10:00" in result.content


async def test_full_digest_assembles_all_sections(agent, monkeypatch):
    _patch_sources(
        monkeypatch,
        weather="+22°C, ясно",
        events="Встреча в 10:00",
        tasks="Сдать отчёт",
        news="🤖 AI-новости дня",
        photo="",
    )
    result = await agent._execute(_ctx())
    for expected in ("+22°C", "Встреча в 10:00", "Сдать отчёт", "AI-новости"):
        assert expected in result.content


async def test_exception_in_one_source_does_not_break_others(agent, monkeypatch):
    """asyncio.gather(..., return_exceptions=True) — одна упавшая ветка не должна ронять весь дайджест."""
    monkeypatch.setattr(morning_agent, "_get_weather", AsyncMock(side_effect=RuntimeError("сеть недоступна")))
    monkeypatch.setattr(morning_agent, "_get_events", AsyncMock(return_value="Встреча в 10:00"))
    monkeypatch.setattr(morning_agent, "_get_tasks", AsyncMock(return_value=""))
    monkeypatch.setattr(morning_agent, "_get_news_digest", AsyncMock(return_value=""))
    monkeypatch.setattr(morning_agent, "_get_photo_schedule", AsyncMock(return_value=""))

    result = await agent._execute(_ctx())
    assert result.success is True
    assert "Встреча в 10:00" in result.content


# ── Хелперы напрямую (реальные запросы к БД, без моков) ───────────────────────

async def test_get_events_returns_empty_when_nothing_scheduled(temp_db):
    from datetime import timezone
    now_msk = datetime.now(timezone.utc) + timedelta(hours=3)
    text = await morning_agent._get_events(1, now_msk)
    assert text == "" or "нет" in text.lower()


async def test_get_events_includes_todays_event(temp_db):
    from datetime import timezone
    from app.calendar_service import create_event

    now_msk = datetime.now(timezone.utc) + timedelta(hours=3)
    today = now_msk.strftime("%Y-%m-%d")
    create_event(1, today, "Встреча с командой", time_start="10:00")

    text = await morning_agent._get_events(1, now_msk)
    assert "Встреча с командой" in text


async def test_get_tasks_empty_state(temp_db):
    text = await morning_agent._get_tasks(1)
    assert "нет" in text.lower()


async def test_get_tasks_prioritizes_overdue_first(temp_db):
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    temp_db.save_task(1, "Просроченная задача", priority=3, due_date=yesterday)
    temp_db.save_task(1, "Задача без срока", priority=1)

    text = await morning_agent._get_tasks(1)
    # Просроченная должна идти раньше в тексте, несмотря на более низкий приоритет
    assert text.index("Просроченная задача") < text.index("Задача без срока")
