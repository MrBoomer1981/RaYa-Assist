"""
test_registry.py — реестр агентов: код/obsidian отсутствуют, фичефлаги работают.
"""
from app.agents.registry import AGENTS, get_enabled_agents


def test_code_and_obsidian_agents_are_gone():
    assert "code" not in AGENTS
    assert "obsidian" not in AGENTS


def test_expected_agents_present():
    expected = {"raya", "todo", "calendar", "diary", "ideas",
                "deep_research", "morning", "explain", "critic"}
    assert expected.issubset(AGENTS.keys())


def test_every_enabled_agent_actually_instantiates_via_registry():
    """
    create_agent() делает динамический import по module/class_name из AGENTS
    и ТИХО глотает исключения, возвращая None при любом несовпадении
    (опечатка в имени класса, устаревший путь после переименования и т.д.).
    В таком случае бот не падает и не логирует явную ошибку пользователю —
    просто молча откатывается на общего 'raya' навсегда для этого интента.
    Конструирование каждого агента напрямую (как в test_*_agent.py) НЕ ловит
    такую рассинхронизацию — только реальный create_agent() ловит.
    """
    from app.agents.registry import create_agent

    failed = []
    for name, info in AGENTS.items():
        if not info.enabled or not info.module:
            continue
        agent = create_agent(name)
        if agent is None:
            failed.append(name)
    assert not failed, f"create_agent() тихо вернул None для: {failed}"


def test_get_enabled_agents_respects_module_toggle(temp_settings):
    temp_settings.update("module_ideas", False)
    enabled_names = {a.name for a in get_enabled_agents()}
    assert "ideas" not in enabled_names

    temp_settings.update("module_ideas", True)
    enabled_names = {a.name for a in get_enabled_agents()}
    assert "ideas" in enabled_names


def test_get_enabled_agents_never_includes_removed_agents(temp_settings):
    enabled_names = {a.name for a in get_enabled_agents()}
    assert "code" not in enabled_names
    assert "obsidian" not in enabled_names
