"""
test_memory_archival.py — Archival Memory: поиск по DEEper KB и форматирование.

Регрессия: раньше search() дополнительно обращался к app.services.obsidian
(теперь удалён) — здесь проверяем, что поиск работает только через DEEper
и нигде не пытается импортировать несуществующий модуль.
"""
from unittest.mock import MagicMock


import app.services.memory.archival as archival


async def test_format_for_prompt_empty_results_returns_empty_string():
    assert archival.format_for_prompt([]) == ""


async def test_format_for_prompt_includes_source_and_title():
    results = [{"source": "DEEper", "title": "Квантовые компьютеры", "snippet": "Кубиты вместо битов", "relevance": 0.9}]
    text = archival.format_for_prompt(results)
    assert "DEEper" in text
    assert "Квантовые компьютеры" in text
    assert "Кубиты вместо битов" in text


async def test_format_for_prompt_never_mentions_obsidian():
    """Регрессия: источник 'Obsidian' в архиве больше существовать не должен."""
    results = [{"source": "DEEper", "title": "Тест", "snippet": "содержимое", "relevance": 0.5}]
    text = archival.format_for_prompt(results)
    assert "obsidian" not in text.lower()


async def test_search_uses_only_deeper_source(monkeypatch):
    """
    Регрессия: search() раньше дополнительно опрашивал app.services.obsidian
    (удалённый модуль). Мокаем bridge и убеждаемся, что всё приходит только
    с source='DEEper' и не падает.
    """
    fake_bridge = MagicMock()
    fake_bridge.search_kb.return_value = [
        {"title": "Исследование про космос", "summary": "Развёрнутый текст про космос", "score": 0.8},
    ]
    monkeypatch.setattr(
        "app.agents.deep_research_agent._get_bridge", lambda: fake_bridge
    )

    results = await archival.search("космос", limit=5)
    assert len(results) == 1
    assert results[0]["source"] == "DEEper"
    assert results[0]["title"] == "Исследование про космос"


async def test_search_returns_empty_list_when_bridge_unavailable(monkeypatch):
    def _raise():
        raise RuntimeError("DEEper не настроен")
    monkeypatch.setattr("app.agents.deep_research_agent._get_bridge", _raise)

    results = await archival.search("что угодно")
    assert results == []


async def test_no_obsidian_module_reference_left_in_archival_source():
    """Статическая регрессия: убеждаемся что модуль не ссылается на удалённый app.services.obsidian."""
    import inspect
    source = inspect.getsource(archival)
    assert "app.services.obsidian" not in source
