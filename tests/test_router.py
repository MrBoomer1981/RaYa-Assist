"""
test_router.py — маршрутизация: ключевые слова, LLM-классификатор, fallback.
"""
import json

import pytest
from unittest.mock import AsyncMock, MagicMock

from app.agents.router import RouterAgent


@pytest.fixture
def router():
    return RouterAgent()


def _mock_llm(response) -> MagicMock:
    """
    ChatGroq — pydantic-модель, нельзя подменить .ainvoke прямо на инстансе
    (pydantic запрещает произвольные атрибуты). Подменяем всю ссылку _llm.
    """
    return MagicMock(ainvoke=AsyncMock(return_value=response))


async def test_conversational_message_routes_to_raya_without_llm(router):
    result = await router.route("привет как дела")
    assert result.agent_name == "raya"
    assert result.used_llm is False


async def test_keyword_match_routes_to_todo_without_llm(router, monkeypatch):
    result = await router.route("добавь задачу купить молоко")
    assert result.agent_name == "todo"
    assert result.used_llm is False


async def test_llm_classification_used_for_ambiguous_message(router, llm_response, monkeypatch):
    monkeypatch.setattr("app.agents.router.quick_match", lambda msg: None)
    router._llm = _mock_llm(llm_response(json.dumps({
        "agent": "explain", "confidence": 0.8, "reason": "объяснение концепции",
    })))
    result = await router.route("нечто максимально нейтральное без явных сигналов ни для одного агента")
    assert result.agent_name == "explain"
    assert result.used_llm is True


async def test_llm_unknown_agent_falls_back_to_raya(router, llm_response, monkeypatch):
    """Регрессия на будущее: если LLM когда-нибудь вернёт 'code' или 'obsidian' — fallback на raya."""
    monkeypatch.setattr("app.agents.router.quick_match", lambda msg: None)
    router._llm = _mock_llm(llm_response(json.dumps({
        "agent": "obsidian", "confidence": 0.9, "reason": "устаревший агент",
    })))
    result = await router.route("что-то совсем неоднозначное про заметки в целом")
    assert result.agent_name == "raya"


async def test_llm_malformed_json_falls_back_to_raya(router, llm_response, monkeypatch):
    monkeypatch.setattr("app.agents.router.quick_match", lambda msg: None)
    router._llm = _mock_llm(llm_response("это не json вообще"))
    result = await router.route("максимально неоднозначный запрос без явных ключевых слов")
    assert result.agent_name == "raya"
    assert result.used_llm is False


async def test_llm_exception_falls_back_to_raya(router, monkeypatch):
    monkeypatch.setattr("app.agents.router.quick_match", lambda msg: None)
    router._llm = MagicMock(ainvoke=AsyncMock(side_effect=RuntimeError("Groq недоступен")))
    result = await router.route("ещё один неоднозначный запрос без явных признаков")
    assert result.agent_name == "raya"
    assert result.used_llm is False
