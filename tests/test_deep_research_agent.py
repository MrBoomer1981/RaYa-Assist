"""
test_deep_research_agent.py — мост между RaYa и DEEper.

test_successful_research_returns_report_with_footer — регрессия критического
бага: раньше код читал переменную `obs_path` (для Obsidian-заметки) ДО того,
как она была присвоена — `UnboundLocalError` вылетал на КАЖДОМ запросе DEEper,
то есть флагманская фича глубокого исследования не работала вообще.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.agents.deep_research_agent as dra_module
from app.agents.base_agent import AgentContext
from app.agents.deep_research_agent import DeepResearchAgent


@pytest.fixture
def agent():
    return DeepResearchAgent()


def _ctx(message: str, mode: str | None = None) -> AgentContext:
    extra = {"deeper_mode": mode} if mode else {}
    return AgentContext(user_id=1, message=message, user_name="tester", extra=extra)


def _fake_bridge(research_result: dict | None = None, research_error: Exception | None = None):
    bridge = MagicMock()
    if research_error:
        bridge.research = AsyncMock(side_effect=research_error)
    else:
        bridge.research = AsyncMock(return_value=research_result or {})
    return bridge


async def test_short_query_returns_clarification_without_calling_bridge(agent, monkeypatch):
    get_bridge = MagicMock()
    monkeypatch.setattr(dra_module, "_get_bridge", get_bridge)

    result = await agent._execute(_ctx("привет"))
    assert result.success is True
    assert "тему" in result.content.lower() or "вопрос" in result.content.lower()
    get_bridge.assert_not_called()


async def test_successful_research_returns_report_with_footer(agent, monkeypatch):
    """
    Регрессия: этот сценарий раньше падал с UnboundLocalError на obs_path
    ДО того как дело доходило до формирования результата.
    """
    bridge = _fake_bridge(research_result={
        "report": "Квантовые компьютеры используют кубиты вместо битов.",
        "sources": ["https://example.com/a", "https://example.com/b"],
        "id": 42,
    })
    monkeypatch.setattr(dra_module, "_get_bridge", lambda: bridge)

    result = await agent._execute(_ctx("расскажи подробно про квантовые компьютеры"))
    assert result.success is True
    assert "Квантовые компьютеры используют кубиты" in result.content
    assert "ID: 42" in result.content
    assert "2 источников" in result.content
    assert result.metadata["deeper_id"] == 42
    assert result.metadata["sources"] == ["https://example.com/a", "https://example.com/b"]


async def test_bridge_init_failure_returns_graceful_error(agent, monkeypatch):
    def _raise():
        raise RuntimeError("GROQ_API_KEY не задан")
    monkeypatch.setattr(dra_module, "_get_bridge", _raise)

    result = await agent._execute(_ctx("расскажи подробно о нейросетях и их истории"))
    assert result.success is False
    assert "GROQ_API_KEY" in result.content


async def test_research_call_failure_returns_graceful_error_with_progress(agent, monkeypatch):
    bridge = _fake_bridge(research_error=RuntimeError("Tavily недоступен"))
    monkeypatch.setattr(dra_module, "_get_bridge", lambda: bridge)

    result = await agent._execute(_ctx("глубокое исследование по теме термоядерного синтеза"))
    assert result.success is False
    assert "Tavily недоступен" in result.content
    assert "progress" in result.metadata


async def test_invalid_mode_falls_back_to_default(agent, monkeypatch):
    bridge = _fake_bridge(research_result={"report": "Отчёт готов", "sources": [], "id": 1})
    monkeypatch.setattr(dra_module, "_get_bridge", lambda: bridge)

    result = await agent._execute(_ctx("расскажи максимально подробно о вулканах", mode="не_существующий_режим"))
    assert result.success is True
    assert result.metadata["deeper_mode"] == "deep"  # дефолтный режим


async def test_valid_mode_from_extra_is_respected(agent, monkeypatch):
    bridge = _fake_bridge(research_result={"report": "Быстрый отчёт", "sources": [], "id": 2})
    monkeypatch.setattr(dra_module, "_get_bridge", lambda: bridge)

    result = await agent._execute(_ctx("расскажи максимально подробно о лисах", mode="simple"))
    assert result.metadata["deeper_mode"] == "simple"
