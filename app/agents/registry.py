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
    parallelizable: bool = False
    needs_critic:   bool = False
    enabled:        bool = True


AGENTS: dict[str, AgentInfo] = {

    "raya": AgentInfo(
        name="raya", title="RaYa",
        description=(
            "Главный ассистент. Отвечает на общие вопросы, ведёт диалог. "
            "Используется по умолчанию если нет явного триггера."
        ),
        keywords=(),
    ),

    "code": AgentInfo(
        name="code", title="Code Agent",
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
        ),
        needs_critic=True,
    ),

    "todo": AgentInfo(
        name="todo", title="Todo Agent",
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

    "calendar": AgentInfo(
        name="calendar", title="Calendar Agent",
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
    ),
}


def get_agent(name: str) -> AgentInfo | None:
    return AGENTS.get(name)


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
