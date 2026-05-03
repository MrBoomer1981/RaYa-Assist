"""
agent_registry.py — реестр всех агентов системы.
"""
from dataclasses import dataclass
from app.feature_flags import FEATURE_IMAGE_AGENT, FEATURE_IDEAS_AGENT


@dataclass(frozen=True)
class AgentInfo:
    name:           str
    title:          str
    description:    str
    keywords:       tuple[str, ...]
    module:         str  = ""    # путь к модулю: "app.agents.code_agent"
    class_name:     str  = ""    # имя класса: "CodeAgent"
    parallelizable: bool = False
    needs_critic:   bool = False
    enabled:        bool = True


AGENTS: dict[str, AgentInfo] = {

    "raya": AgentInfo(
        name="raya", title="RaYa",
        module="app.agents.raya_agent", class_name="RayaAgent",
        description=(
            "Главный ассистент. Отвечает на общие вопросы, ведёт диалог. "
            "Используется по умолчанию если нет явного триггера."
        ),
        keywords=(),
    ),

    "code": AgentInfo(
        name="code", title="Code Agent",
        module="app.agents.code_agent", class_name="CodeAgent",
        description=(
            "Пишет, отлаживает, объясняет код. Архитектура, code review, "
            "рефакторинг. Python, JavaScript, SQL, bash и другие языки."
        ),
        keywords=(
            "код", "code", "python", "javascript", "функци", "класс",
            "баг", "ошибк", "debug", "напиши скрипт", "реализуй",
            "рефактор", "sql", "запрос", "программ",
        ),
        parallelizable=True, needs_critic=True,
    ),

    "image": AgentInfo(
        name="image", title="Image Agent",
        module="app.agents.image_agent", class_name="ImageAgent",
        description="Генерирует изображения по текстовому описанию через FLUX.",
        keywords=(
            "нарисуй", "сгенерируй картинк", "создай изображени",
            "draw", "generate image", "картинк", "изображени", "визуализируй",
        ),
        parallelizable=True,
        enabled=FEATURE_IMAGE_AGENT,
    ),

    "research": AgentInfo(
        name="research", title="Research Agent",
        module="app.agents.research_agent", class_name="ResearchAgent",
        description=(
            "Исследует темы, проверяет факты, анализирует научные данные. "
            "Три режима: research (глубокое исследование), "
            "fact_check (проверка утверждения — за/против), "
            "science (верификация научных данных с источниками)."
        ),
        keywords=(
            "исследуй", "изучи", "найди информацию", "что известно",
            "это правда", "верно ли", "проверь факт", "миф или",
            "на самом деле", "за и против", "плюсы и минусы",
            "научн", "исследовани", "докажи", "источник", "достоверн",
            "статистик", "данные говорят", "обзор темы",
            "reddit", "реддит", "что на реддите", "горячее на reddit",
            "twitter", "x.com", "твиттер", "тренды в твиттере", "тренды на x",
            "что обсуждают", "горячие темы", "тренды", "что сейчас обсуждают",
            "viral", "trending",
        ),
        needs_critic=True,
    ),

    "todo": AgentInfo(
        name="todo", title="Todo Agent",
        module="app.agents.todo_agent", class_name="TodoAgent",
        description=(
            "Управляет задачами (матрица Эйзенхауэра). "
            "Добавляет, показывает, выполняет, удаляет задачи. "
            "База данных — единственный источник правды."
        ),
        keywords=(
            "задача", "задачи", "todo", "список задач",
            "добавь задачу", "покажи задачи", "мои задачи",
            "выполнено", "выполни задачу", "удали задачу",
            "нужно сделать", "не забыть сделать", "дедлайн",
        ),
        # enabled=True (default)  # re-enabled
    ),



    "text": AgentInfo(
        name="text", title="Text Agent",
        module="app.agents.text_agent", class_name="TextAgent",
        description=(
            "Трансформирует готовый текст: резюмирует, редактирует стиль, меняет тон, "
            "переводит, пишет письма/посты по шаблону. "
            "ТОЛЬКО если пользователь предоставил конкретный текст для обработки "
            "или просит написать конкретный документ. "
            "НЕ для вопросов, разговоров, фактических запросов."
        ),
        keywords=(
            "резюмируй", "сожми текст", "кратко перескажи", "тл;др", "tldr",
            "отредактируй", "улучши текст", "перепиши", "исправь стиль",
            "измени тон", "сделай формальн", "сделай дружелюбн",
            "переведи", "перевод на",
            "напиши письмо", "напиши пост", "составь письмо", "составь резюме",
        ),
        parallelizable=True, needs_critic=True,
    ),

    "ideas": AgentInfo(
        name="ideas", title="Ideas Agent",
        module="app.agents.ideas_agent", class_name="IdeasAgent",
        description=(
            "Генерирует идеи и нестандартные решения. "
            "Брейнсторм, SCAMPER, обратный брейнсторм, devil's advocate."
        ),
        keywords=(
            "идеи для", "придумай идеи", "брейнсторм", "накидай варианты",
            "как придумать", "нестандартн", "креативн",
            "а что если", "альтернативы", "scamper",
        ),
        parallelizable=True,
        enabled=FEATURE_IDEAS_AGENT,
    ),

    "explain": AgentInfo(
        name="explain", title="Explain & Plan Agent",
        module="app.agents.explain_agent", class_name="ExplainAgent",
        description=(
            "Объясняет концепции, структурирует информацию, разбивает на шаги, "
            "строит планы с дедлайнами и рисками. "
            "Режимы: explain, structure, breakdown, plan."
        ),
        keywords=(
            "объясни подробно", "что такое", "как работает", "не понимаю",
            "простыми словами", "структурируй", "упорядочи", "выдели главное",
            "пошагово", "инструкция", "как сделать",
            "составь план", "распланируй", "декомпозиция", "roadmap",
            "план на", "план по", "как реализовать", "тайм-менеджмент",
        ),
        needs_critic=True,
    ),

    "deep_research": AgentInfo(
        name="deep_research", title="Deep Research",
        module="app.agents.deep_research_agent", class_name="DeepResearchAgent",
        description=(
            "Глубокое многошаговое исследование темы в стиле Perplexity Deep Research. "
            "Декомпозирует вопрос, ищет параллельно по нескольким направлениям, "
            "заполняет пробелы, синтезирует структурированный отчёт с источниками. "
            "Занимает 30-90 секунд. Используй для сложных аналитических вопросов."
        ),
        keywords=(
            "глубокое исследование", "deep research", "подробный анализ",
            "исследуй глубоко", "детальный отчёт", "полный анализ",
            "всё о", "расскажи подробно всё", "подготовь отчёт",
            "аналитика по", "детально изучи", "развёрнутый анализ",
        ),
    ),

    "diary": AgentInfo(
        name="diary", title="Diary Agent",
        module="app.agents.diary_agent", class_name="DiaryAgent",
        description=(
            "Ведёт личный дневник. Записывает мысли, переживания, события дня. "
            "Показывает записи, делает рефлексию по паттернам и настроению. "
            "Автоматически определяет настроение и сохраняет в mood_log."
        ),
        keywords=(
            "запиши в дневник", "дневник", "хочу записать", "добавь запись",
            "зафиксируй", "сегодня я", "сегодня было", "хочу поделиться",
            "покажи дневник", "мои записи", "что я писал", "рефлексия",
            "проанализируй записи", "что ты заметила", "записи за",
        ),
    ),

    "calendar": AgentInfo(
        name="calendar", title="Calendar Agent",
        module="app.agents.calendar_agent", class_name="CalendarAgent",
        description=(
            "Управляет событиями календаря. "
            "Добавляет, показывает, удаляет, обновляет события. "
            "Знает дату, время, напоминания, расписание, встречи, планы на конкретный день."
        ),
        keywords=(
            "добавь событие", "добавь встречу", "запланируй", "поставь встречу",
            "создай событие", "внеси в календарь", "отметь в календаре",
            "покажи события", "что сегодня", "что завтра", "расписание на",
            "мой календарь", "ближайшие события", "что запланировано",
            "удали событие", "перенеси встречу", "измени время встречи",
            "встреча в", "митинг", "созвон", "дедлайн",
        ),
    ),

    "morning": AgentInfo(
        name="morning", title="Утренний дайджест",
        module="app.agents.morning_agent", class_name="MorningAgent",
        description=(
            "Запускается ТОЛЬКО автоматически в 6:45 МСК. "
            "НЕ выбирать в ответ на сообщения пользователя."
        ),
        keywords=("утренний дайджест", "дайджест на сегодня"),
    ),

    "critic": AgentInfo(
        name="critic", title="Critic Agent",
        description="Финальная проверка ответов других агентов. Только программно.",
        keywords=(),
        module="app.agents.critic_agent", class_name="CriticAgent",
        enabled=False,
    ),
}


def get_agent(name: str) -> AgentInfo | None:
    return AGENTS.get(name)


def create_agent(name: str):
    """
    Создаёт экземпляр агента по имени через реестр.
    Добавление нового агента = только запись в AGENTS выше.
    Orchestrator и router обновятся автоматически.
    """
    info = AGENTS.get(name)
    if info is None or not info.enabled:
        return None
    if not info.module or not info.class_name:
        return None
    try:
        import importlib
        mod = importlib.import_module(info.module)
        cls = getattr(mod, info.class_name)
        return cls()
    except Exception as e:
        import logging
        logging.getLogger(__name__).exception(
            "Ошибка создания агента '%s' (%s.%s): %s",
            name, info.module, info.class_name, e
        )
        return None


def get_enabled_agents() -> list[AgentInfo]:
    return [a for a in AGENTS.values() if a.enabled]


def get_routable_agents() -> list[AgentInfo]:
    excluded = {"critic", "raya", "morning"}
    return [a for a in AGENTS.values() if a.enabled and a.name not in excluded]


def quick_match(message: str) -> str | None:
    msg_lower = message.lower()
    scores: dict[str, int] = {}
    for agent in get_routable_agents():
        count = sum(1 for kw in agent.keywords if kw in msg_lower)
        if count > 0:
            scores[agent.name] = count
    if not scores:
        return None
    best      = max(scores, key=lambda k: scores[k])
    top_score = scores[best]
    if list(scores.values()).count(top_score) > 1:
        return None
    return best
