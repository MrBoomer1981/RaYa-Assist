"""
test_base_agent.py — кэш LLM-клиентов в _get_llm().

test_temperature_updates_on_cached_client — регрессия: temperature раньше
читалась только в момент СОЗДАНИЯ ChatGroq-клиента для конкретной модели.
Клиенты кэшируются по имени модели (_LLM_CACHE) и переиспользуются между
вызовами, поэтому после первого сообщения смена температуры через
/settings молча переставала действовать — кэшированный клиент навсегда
оставался со старым значением, хотя UI показывал новое.
"""
import pytest

from app.agents.base_agent import _get_llm
import app.agents.base_agent as ba_mod


@pytest.fixture(autouse=True)
def _clear_llm_cache():
    """_LLM_CACHE — модульный кэш, общий на процесс; изолируем тесты друг от друга."""
    ba_mod._LLM_CACHE.clear()
    yield
    ba_mod._LLM_CACHE.clear()


def test_temperature_updates_on_already_cached_client(temp_settings):
    temp_settings.update("temperature", 0.3)
    llm1 = _get_llm("llama-3.3-70b-versatile")
    assert llm1.temperature == 0.3

    temp_settings.update("temperature", 1.6)
    llm2 = _get_llm("llama-3.3-70b-versatile")

    assert llm2.temperature == 1.6, "смена /settings должна применяться даже к уже закэшированному клиенту"
    assert llm1 is llm2, "клиент должен переиспользоваться, а не пересоздаваться на каждый вызов"


def test_different_models_get_independent_cache_entries(temp_settings):
    temp_settings.update("temperature", 0.5)
    llm_a = _get_llm("model-a")
    llm_b = _get_llm("model-b")

    assert llm_a is not llm_b
    assert llm_a.temperature == 0.5
    assert llm_b.temperature == 0.5


def test_cache_evicts_oldest_when_full(temp_settings, monkeypatch):
    monkeypatch.setattr(ba_mod, "_LLM_CACHE_MAX", 2)
    temp_settings.update("temperature", 0.5)

    _get_llm("model-a")
    _get_llm("model-b")
    _get_llm("model-c")  # должен вытеснить model-a (самый старый — вставлен первым)

    assert "model-a" not in ba_mod._LLM_CACHE
    assert "model-b" in ba_mod._LLM_CACHE
    assert "model-c" in ba_mod._LLM_CACHE
