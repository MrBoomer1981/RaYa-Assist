"""
test_orchestrator.py — координация: роутинг → агент → критик → очистка ответа.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.agents.base_agent import AgentResult
from app.agents.orchestrator import Orchestrator
from app.agents.router import RouteResult
from app.services.memory.manager import MemoryContext


@pytest.fixture
def orchestrator(temp_db):
    orch = Orchestrator()
    orch._memory.build_context = AsyncMock(return_value=MemoryContext())
    orch._memory.after_turn = AsyncMock(return_value=None)
    return orch


def _route(agent_name: str):
    return RouteResult(agent_name=agent_name, confidence=0.9, used_llm=False, reason="test")


async def test_run_routes_to_agent_and_returns_reply(orchestrator, monkeypatch):
    orchestrator._router.route = AsyncMock(return_value=_route("todo"))
    fake_agent = MagicMock()
    fake_agent.run = AsyncMock(return_value=AgentResult(
        success=True, content="Задача добавлена.", agent_name="todo", needs_critic=False,
    ))
    orchestrator._get_agent = MagicMock(return_value=fake_agent)

    result = await orchestrator.run(user_id=1, message="добавь задачу купить хлеб")
    assert result.success is True
    assert result.content == "Задача добавлена."
    fake_agent.run.assert_awaited_once()


async def test_run_invokes_critic_when_needed(orchestrator):
    orchestrator._router.route = AsyncMock(return_value=_route("explain"))
    fake_agent = MagicMock()
    fake_agent.run = AsyncMock(return_value=AgentResult(
        success=True, content="Черновой ответ.", agent_name="explain", needs_critic=True,
    ))
    orchestrator._get_agent = MagicMock(return_value=fake_agent)

    fake_critic = MagicMock()
    fake_critic.review = AsyncMock(return_value=AgentResult(
        success=True, content="Улучшенный ответ после критика.", agent_name="explain",
    ))
    orchestrator._get_critic = MagicMock(return_value=fake_critic)

    result = await orchestrator.run(user_id=1, message="объясни квантовую запутанность")
    assert result.content == "Улучшенный ответ после критика."
    fake_critic.review.assert_awaited_once()


async def test_critic_failure_falls_back_to_original_result(orchestrator):
    """Если критик упал — отдаём оригинальный ответ агента, а не падаем сами."""
    orchestrator._router.route = AsyncMock(return_value=_route("explain"))
    fake_agent = MagicMock()
    fake_agent.run = AsyncMock(return_value=AgentResult(
        success=True, content="Оригинальный ответ.", agent_name="explain", needs_critic=True,
    ))
    orchestrator._get_agent = MagicMock(return_value=fake_agent)

    fake_critic = MagicMock()
    fake_critic.review = AsyncMock(side_effect=RuntimeError("критик недоступен"))
    orchestrator._get_critic = MagicMock(return_value=fake_critic)

    result = await orchestrator.run(user_id=1, message="объясни теорию относительности")
    assert result.content == "Оригинальный ответ."


async def test_missing_agent_falls_back_to_raya(orchestrator):
    """Регрессия на будущее: маршрут на несуществующего агента (например, старый 'code') не должен падать."""
    orchestrator._router.route = AsyncMock(return_value=_route("code"))  # агент больше не существует
    raya_agent = MagicMock()
    raya_agent.run = AsyncMock(return_value=AgentResult(
        success=True, content="Общий ответ от RaYa.", agent_name="raya",
    ))

    def _get_agent(name):
        return None if name == "code" else raya_agent
    orchestrator._get_agent = MagicMock(side_effect=_get_agent)

    result = await orchestrator.run(user_id=1, message="что-то неоднозначное")
    assert result.content == "Общий ответ от RaYa."
