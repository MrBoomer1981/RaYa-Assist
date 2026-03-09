"""
agent_registry.py — реестр всех агентов системы.

RaYa читает этот реестр чтобы знать:
- какие агенты существуют
- когда их использовать
- что они умеют

Добавить нового агента = добавить одну запись в AGENTS.
"""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class AgentInfo:
    """Описание одного агента."""

    # Уникальный идентификатор — используется в коде
    name: str

    # Человекочитаемое название
    title: str

    # Когда роутер должен выбрать этого агента
    # Чем точнее — тем лучше маршрутизация
    description: str

    # Ключевые слова-триггеры для быстрой маршрутизации без LLM
    keywords: tuple[str, ...]

    # Может ли агент работать параллельно с другими
    parallelizable: bool = False

    # Нужен ли финальный критик после этого агента
    needs_critic: bool = False

    # Метаданные — версия, статус
    enabled: bool = True


# ── Реестр агентов ────────────────────────────────────────────────────────────

AGENTS: dict[str, AgentInfo] = {

    "raya": AgentInfo(
        name="raya",
        title="RaYa",
        description=(
            "Главный оркестратор. Отвечает на общие вопросы, "
            "ведёт диалог, координирует других агентов. "
            "Используется по умолчанию если нет явного триггера."
        ),
        keywords=(),  # fallback — используется если никто другой не подошёл
        parallelizable=False,
        needs_critic=False,
    ),

    "code": AgentInfo(
        name="code",
        title="Code Agent",
        description=(
            "Специалист по коду. Пишет, отлаживает, объясняет код. "
            "Архитектурные решения, code review, рефакторинг. "
            "Языки: Python, JavaScript, SQL, bash и другие."
        ),
        keywords=(
            "код", "code", "python", "javascript", "функци", "класс",
            "баг", "ошибк", "debug", "напиши скрипт", "реализуй",
            "рефактор", "архитектур", "sql", "запрос", "алгоритм",
        ),
        parallelizable=True,
        needs_critic=True,
    ),

    "image": AgentInfo(
        name="image",
        title="Image Agent",
        description=(
            "Генерирует изображения по текстовому описанию. "
            "Использует Hugging Face FLUX модель. Бесплатно."
        ),
        keywords=(
            "нарисуй", "сгенерируй картинк", "создай изображени",
            "draw", "generate image", "картинк", "изображени",
            "нарисуй мне", "визуализируй",
        ),
        parallelizable=True,
        needs_critic=False,
    ),

    "diary": AgentInfo(
        name="diary",
        title="Diary Agent",
        description=(
            "Личный дневник. Принимает записи, помогает с рефлексией, "
            "анализирует настроение и паттерны. Данные приватны — "
            "не передаются другим агентам."
        ),
        keywords=(
            "дневник", "запиши", "запись", "сегодня я", "чувствую",
            "настроени", "рефлексия", "личное", "diary", "journal",
            "хочу записать", "не забудь что я",
        ),
        parallelizable=False,
        needs_critic=False,
    ),

    "science": AgentInfo(
        name="science",
        title="Science Agent",
        description=(
            "Проверяет научные факты, находит источники, анализирует данные. "
            "Использует поиск для актуальной информации. "
            "Всегда указывает источники и степень достоверности."
        ),
        keywords=(
            "исследовани", "научн", "докажи", "источник", "факт",
            "проверь", "достоверн", "статистик", "данные говорят",
            "согласно", "science", "research", "study", "правда ли",
        ),
        parallelizable=True,
        needs_critic=True,
    ),

    "todo": AgentInfo(
        name="todo",
        title="Todo агент",
        description="Управление задачами: добавить, показать, выполнить, удалить. Приоритеты и дедлайны.",
        keywords=(
            "задача", "задачи", "todo", "сделать", "напомни сделать",
            "добавь задачу", "список дел", "выполнено", "выполни задачу",
            "удали задачу", "покажи задачи", "мои задачи", "дедлайн",
        ),
        parallelizable=False,
        needs_critic=False,
        enabled=True,
    ),
    "morning": AgentInfo(
        name="morning",
        title="Утренний дайджест",
        description="Утренний дайджест: погода, tech-новости, задачи, напоминания.",
        keywords=(
            "утренний дайджест", "дайджест", "что сегодня", "погода сегодня",
        ),
        parallelizable=False,
        needs_critic=False,
        enabled=True,
    ),
    "critic": AgentInfo(
        name="critic",
        title="Critic Agent",
        description=(
            "Финальная проверка результатов других агентов. "
            "Ищет ошибки, неточности, улучшения. "
            "Не генерирует контент — только оценивает."
        ),
        keywords=(),  # вызывается только программно, не роутером
        parallelizable=False,
        needs_critic=False,
        enabled=True,
    ),

}


# ── Вспомогательные функции ───────────────────────────────────────────────────

def get_agent(name: str) -> AgentInfo | None:
    """Возвращает агента по имени или None."""
    return AGENTS.get(name)


def get_enabled_agents() -> list[AgentInfo]:
    """Возвращает список всех активных агентов."""
    return [a for a in AGENTS.values() if a.enabled]


def get_routable_agents() -> list[AgentInfo]:
    """
    Возвращает агентов которых роутер может выбирать.
    Исключает critic (вызывается только программно) и raya (fallback).
    """
    excluded = {"critic", "raya"}
    return [a for a in AGENTS.values() if a.enabled and a.name not in excluded]


def quick_match(message: str) -> str | None:
    """
    Быстрая маршрутизация по ключевым словам — без LLM запроса.
    Возвращает имя агента или None если нужен LLM роутер.
    """
    msg_lower = message.lower()
    scores: dict[str, int] = {}

    for agent in get_routable_agents():
        count = sum(1 for kw in agent.keywords if kw in msg_lower)
        if count > 0:
            scores[agent.name] = count

    if not scores:
        return None  # нужен LLM роутер

    # Выбираем агента с наибольшим числом совпадений
    best = max(scores, key=lambda k: scores[k])

    # Если несколько агентов с одинаковым счётом — неоднозначно, идём в LLM
    top_score = scores[best]
    if list(scores.values()).count(top_score) > 1:
        return None

    return best
