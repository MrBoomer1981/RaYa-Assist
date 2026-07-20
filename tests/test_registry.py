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


# ── get_routable_agents / quick_match / create_agent — тот же тумблер ────────
# Регрессия: раньше get_routable_agents() (то, что РЕАЛЬНО использует
# RouterAgent — и keyword-матч, и LLM-промпт) не проверял /settings-тумблеры
# модулей вообще. get_enabled_agents() их проверял, но использовался только
# для строчки в логах при старте. В итоге выключение модуля в /settings
# визуально срабатывало, но роутер продолжал направлять сообщения в
# "выключенный" агент как ни в чём не бывало.

def test_get_routable_agents_respects_module_toggle(temp_settings):
    from app.agents.registry import get_routable_agents

    temp_settings.update("module_todo", False)
    routable_names = {a.name for a in get_routable_agents()}
    assert "todo" not in routable_names

    temp_settings.update("module_todo", True)
    routable_names = {a.name for a in get_routable_agents()}
    assert "todo" in routable_names


def test_quick_match_does_not_route_to_disabled_module(temp_settings):
    from app.agents.registry import quick_match

    temp_settings.update("module_todo", False)
    # Однозначная фраза про задачи — раньше всё равно матчилась бы на todo
    assert quick_match("добавь задачу купить молоко") != "todo"

    temp_settings.update("module_todo", True)
    assert quick_match("добавь задачу купить молоко") == "todo"


def test_create_agent_refuses_disabled_module_as_defense_in_depth(temp_settings):
    """
    Основной фикс — в роутере, но create_agent() тоже не должен создавать
    инстанс агента, выключенного в /settings, даже если что-то другое
    (не роутер) попробует его вызвать напрямую по имени.
    """
    from app.agents.registry import create_agent

    temp_settings.update("module_calendar", False)
    assert create_agent("calendar") is None

    temp_settings.update("module_calendar", True)
    assert create_agent("calendar") is not None


def test_routable_agents_still_excludes_raya_morning_critic(temp_settings):
    """Эти три не должны попадать в список роутинга независимо от тумблеров."""
    from app.agents.registry import get_routable_agents

    routable_names = {a.name for a in get_routable_agents()}
    assert "raya" not in routable_names
    assert "morning" not in routable_names
    assert "critic" not in routable_names
