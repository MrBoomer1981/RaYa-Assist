"""
test_todo_agent.py — парсинг тегов <tasks>/<done>/<delete>, мутации в БД.

Регрессия: тег <longterm> убран полностью (раньше сохранялся ТОЛЬКО в Obsidian
и терялся безвозвратно после его удаления) — теперь такие цели уходят как
обычная Q2-задача и реально сохраняются в SQLite.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.agents.base_agent import AgentContext
from app.agents.todo_agent import TodoAgent


@pytest.fixture
def agent():
    return TodoAgent()


def _ctx(message: str) -> AgentContext:
    return AgentContext(user_id=1, message=message, user_name="tester")


def _with_response(agent: TodoAgent, text: str) -> None:
    agent._llm = MagicMock(ainvoke=AsyncMock(return_value=MagicMock(content=text)))


async def test_add_task_persists_to_db(agent, temp_db):
    _with_response(agent, 'Добавила! <tasks>[{"quadrant":"q2","tasks":["Купить молоко"]}]</tasks>')
    result = await agent._execute(_ctx("добавь задачу купить молоко"))
    assert result.success is True
    assert "<tasks>" not in result.content  # тег убран из ответа пользователю

    tasks = temp_db.get_active_tasks(1)
    assert any(t[1] == "Купить молоко" for t in tasks)


async def test_longterm_goal_now_saved_as_regular_task(agent, temp_db):
    """
    Раньше <longterm> уходил только в Obsidian. Модель теперь проинструктирована
    заворачивать долгосрочные цели в обычный <tasks q2> — они реально сохраняются.
    """
    _with_response(agent, 'Записала мечту! <tasks>[{"quadrant":"q2","tasks":["Выучить испанский за 2 года"]}]</tasks>')
    result = await agent._execute(_ctx("хочу когда-нибудь выучить испанский"))
    assert result.success is True
    tasks = temp_db.get_active_tasks(1)
    assert any("испанский" in t[1] for t in tasks)


async def test_done_marks_task_complete(agent, temp_db):
    task_id = temp_db.save_task(1, "Сдать отчёт", priority=1)

    _with_response(agent, "Отлично, отмечаю! <done>Сдать отчёт</done>")
    result = await agent._execute(_ctx("я сдал отчёт"))
    assert result.success is True
    assert "<done>" not in result.content

    active = temp_db.get_active_tasks(1)
    assert all(t[0] != task_id for t in active)


async def test_delete_removes_task(agent, temp_db):
    task_id = temp_db.save_task(1, "Ненужная задача", priority=3)

    _with_response(agent, "Удалила. <delete>Ненужная задача</delete>")
    result = await agent._execute(_ctx("удали ненужную задачу"))
    assert result.success is True
    active = temp_db.get_active_tasks(1)
    assert all(t[0] != task_id for t in active)


async def test_show_tasks_uses_fast_path_without_llm(agent, temp_db):
    """'покажи задачи' и подобные фразы — прямой ответ из БД, LLM не вызывается."""
    temp_db.save_task(1, "Уже существующая задача", priority=2)
    agent._llm = MagicMock(ainvoke=AsyncMock(side_effect=AssertionError("LLM не должен вызываться")))
    result = await agent._execute(_ctx("покажи мои задачи"))
    assert result.success is True
    assert "Уже существующая задача" in result.content


async def test_plain_reply_without_tags_passes_through(agent, temp_db):
    """Сообщение без ключевых слов списка идёт через LLM; ответ без тегов не ломает парсинг."""
    _with_response(agent, "Окей, поняла тебя!")
    result = await agent._execute(_ctx("ладно, отложим это на потом"))
    assert result.success is True
    assert "Окей, поняла тебя!" in result.content
