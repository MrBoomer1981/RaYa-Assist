"""
test_calendar_agent.py — добавление/удаление/важность событий.

test_add_event_persists_to_db — регрессия критического бага: save_event()
раньше перечислял 10 колонок в INSERT, но передавал только 7 значений
(importance/repeat/remind_days терялись), из-за чего ЛЮБОЕ добавление
события падало с 'OperationalError: 7 values for 10 columns'.
"""
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.agents.base_agent import AgentContext
from app.agents.calendar_agent import CalendarAgent


@pytest.fixture
def agent():
    return CalendarAgent()


def _ctx(message: str) -> AgentContext:
    return AgentContext(user_id=1, message=message, user_name="tester")


def _with_response(agent: CalendarAgent, text: str) -> None:
    agent._llm = MagicMock(ainvoke=AsyncMock(return_value=MagicMock(content=text)))


async def test_add_event_persists_to_db(agent, temp_db):
    payload = json.dumps({
        "date": "2026-08-01", "title": "День рождения друга",
        "time_start": "18:00", "time_end": "", "description": "",
        "color": "blue", "importance": 1, "repeat": "yearly", "remind_days": 3,
    })
    _with_response(agent, f"Записала! <add_event>{payload}</add_event>")

    result = await agent._execute(_ctx("добавь день рождения друга 1 августа в 18:00, важное"))
    assert result.success is True

    events = temp_db.get_events_for_month(1, 2026, 8)
    assert any(e["title"] == "День рождения друга" for e in events)


async def test_mark_important_updates_importance(agent, temp_db):
    from app.calendar_service import create_event
    event = create_event(1, "2026-08-05", "Обычное событие", time_start="10:00")

    _with_response(agent, f'<mark_important id="{event["id"]}" level="2"/>')
    result = await agent._execute(_ctx("сделай это событие критически важным"))
    assert result.success is True

    with temp_db._conn() as con:
        row = con.execute("SELECT importance FROM events WHERE id=?", (event["id"],)).fetchone()
    assert row[0] == 2


async def test_delete_event_via_tag(agent, temp_db):
    from app.calendar_service import create_event
    event = create_event(1, "2026-08-06", "Удалить меня", time_start="09:00")

    _with_response(agent, f"<delete_event>{event['id']}</delete_event>")
    result = await agent._execute(_ctx(f"удали событие {event['id']}"))
    assert result.success is True

    remaining = temp_db.get_upcoming_events(1, limit=50)
    assert all(e["id"] != event["id"] for e in remaining)


async def test_malformed_add_event_json_does_not_crash(agent, temp_db):
    """Модель иногда возвращает битый JSON — агент должен просто пропустить, а не упасть."""
    _with_response(agent, "<add_event>{не валидный json}</add_event>")
    result = await agent._execute(_ctx("добавь что-то"))
    assert result.success is True


async def test_show_day_includes_events_from_db(agent, temp_db):
    from app.calendar_service import create_event
    create_event(1, "2026-08-10", "Стоматолог", time_start="15:00")

    _with_response(agent, "<show_day>2026-08-10</show_day>")
    result = await agent._execute(_ctx("что у меня 10 августа"))
    assert result.success is True
    assert "Стоматолог" in result.content


async def test_show_week_lists_all_seven_days(agent, temp_db):
    from app.calendar_service import create_event
    create_event(1, "2026-08-10", "Событие в понедельник недели", time_start="09:00")

    _with_response(agent, "<show_week>2026-08-10</show_week>")
    result = await agent._execute(_ctx("покажи неделю"))
    assert result.success is True
    assert "Неделя с" in result.content
    assert "Событие в понедельник недели" in result.content


async def test_show_upcoming_lists_future_events(agent, temp_db):
    from app.calendar_service import create_event
    from datetime import datetime, timedelta
    future = (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%d")
    create_event(1, future, "Ближайшее событие", time_start="12:00")

    _with_response(agent, "<show_upcoming/>")
    result = await agent._execute(_ctx("что у меня ближайшее"))
    assert result.success is True
    assert "Ближайшее событие" in result.content


async def test_search_events_finds_by_title(agent, temp_db):
    from app.calendar_service import create_event
    create_event(1, "2026-08-15", "Встреча с врачом-стоматологом", time_start="10:00")

    _with_response(agent, "<search_events>стоматолог</search_events>")
    result = await agent._execute(_ctx("найди события про стоматолога"))
    assert result.success is True
    assert "стоматологом" in result.content


async def test_update_event_changes_title(agent, temp_db):
    from app.calendar_service import create_event
    event = create_event(1, "2026-08-20", "Старое название встречи", time_start="09:00")

    _with_response(agent, f'<update_event>{{"id": {event["id"]}, "title": "Новое название встречи"}}</update_event>')
    result = await agent._execute(_ctx("переименуй встречу"))
    assert result.success is True

    with temp_db._conn() as con:
        row = con.execute("SELECT title FROM events WHERE id=?", (event["id"],)).fetchone()
    assert row[0] == "Новое название встречи"
