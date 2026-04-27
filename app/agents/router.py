"""
router.py — маршрутизатор задач.

Двухуровневая маршрутизация:
1. Быстрый матч по ключевым словам (без LLM, мгновенно)
2. LLM классификатор (только если ключевые слова не дали однозначного ответа)

Возвращает имя агента который должен обработать сообщение.
"""
import json
import logging
import re
from dataclasses import dataclass

from langchain_core.messages import HumanMessage
from langchain_groq import ChatGroq

from app.utils import strip_json
from app.agents.registry import get_routable_agents, quick_match
from app.config import settings

# ── Уровень 0: разговорные реплики → raya без LLM ─────────────────────────────
# Короткие (<4 слов) эмоциональные/бытовые сообщения не нужно классифицировать.
# LLM всё равно ошибается на них — он видит "текст" и шлёт в text-агент.
_CONVERSATIONAL_RE = re.compile(
    r"^("
    r"ок|окей|okay|ok|"
    r"супер|отлично|класс|круто|здорово|норм|нормально|"
    r"понял|понятно|ясно|ясненько|"
    r"спасибо|спс|благодарю|thanks|thank you|"
    r"привет|хай|hello|hi|добрый|"
    r"пока|до свидания|bye|"
    r"да|нет|может|наверное|"
    r"хорошо|хор|"
    r"молодец|зачёт|огонь|топ|"
    r"лол|хаха|хехе|ха|"
    r"не надо|стоп|отмена|отмени|cancel"
    r")[\s!?.]*$",
    re.IGNORECASE | re.UNICODE,
)

# Фразы которые явно требуют raya (поиск, время, погода, факты)
_RAYA_RE = re.compile(
    r"("
    r"сколько времени|который час|время в|часовой пояс|"
    r"погода|температура|прогноз|"
    r"курс|доллар|евро|биткоин|"
    r"новости|что случилось|что происходит|"
    r"кто такой|кто такая|что такое|где находится|"
    r"когда|почему|зачем|"
    r"расскажи|объясни мне|помоги|посоветуй|"
    r"как дела|что думаешь|твоё мнение"
    r")",
    re.IGNORECASE | re.UNICODE,
)

logger = logging.getLogger(__name__)

# Лёгкая быстрая модель для роутера — не тратим тяжёлую модель на классификацию
_ROUTER_MODEL = settings.router_model

# Голые команды поиска → raya + берём тему из истории
_SEARCH_CMD_RE = re.compile(
    r"^(найди в интернете|поищи в интернете|загугли|найди|поищи|ищи)[!.\s]*$",
    re.IGNORECASE | re.UNICODE,
)



@dataclass(frozen=True)
class RouteResult:
    """Результат маршрутизации."""
    agent_name: str       # имя выбранного агента
    confidence: float     # уверенность 0.0–1.0
    reason: str           # почему выбран этот агент
    used_llm: bool        # использовался ли LLM или быстрый матч


class RouterAgent:
    """
    Определяет какой агент должен обработать сообщение.
    Сначала пробует быстрый матч по ключевым словам,
    при неоднозначности использует лёгкую LLM модель.
    """

    def __init__(self) -> None:
        # Отдельный лёгкий LLM для роутера — не основная модель
        self._llm = ChatGroq(
            api_key=settings.groq_api_key,
            model=_ROUTER_MODEL,
            temperature=0.0,  # детерминированность — нам нужен чёткий выбор
        )
        self._agents = get_routable_agents()
        logger.info(
            "🔀 Роутер инициализирован | агентов: %d | модель: %s",
            len(self._agents), _ROUTER_MODEL,
        )

    def _build_router_prompt(self, message: str, calibration_hint: str | None = None) -> str:
        """
        Формирует промпт для LLM роутера.
        Описания агентов берутся из реестра — не захардкожены.
        При добавлении нового агента в registry.py промпт обновится автоматически.
        """
        agents_desc = "\n".join(
            f'- "{a.name}": {a.description}'
            for a in self._agents
        )
        hint_block = f"\n\nПодсказка: {calibration_hint}" if calibration_hint else ""
        return (
            f"Сообщение: \"{message}\"\n\n"
            f"Агенты:\n{agents_desc}\n\n"
            "Выбери одного агента. Если сомневаешься — \'raya\'.\n\n"
            "ЖЁСТКИЕ ПРАВИЛА:\n"
            "- \'morning\' — НИКОГДА вручную\n"
            "- \'text\' — только если есть конкретный текст для трансформации\n"
            "- \'raya\' — вопросы, факты, погода, новости, разговор, всё остальное\n\n"
            "ПРИМЕРЫ (по одному на агента):\n"
            + "\n".join(
                f"- {ex}"
                for ex in self._routing_examples()
            )
            + f"{hint_block}\n\n"
            "ТОЛЬКО JSON: "
            '{"agent": "<имя>", "confidence": <0.0-1.0>, "reason": "<кратко>"}'
        )

    def _routing_examples(self) -> list[str]:
        """
        Примеры роутинга — генерируются из реестра.
        Каждый агент с keywords даёт первый keyword как пример.
        Добавь examples поле в AgentInfo для кастомных примеров.
        """
        fixed = [
            "'сколько времени в лондоне' → raya",
            "'перепиши письмо деловым тоном' → text",
            "'объясни TCP' → explain",
            "'напиши функцию сортировки' → code",
            "'добавь задачу' → todo",
            "'запиши в дневник' → diary",
            "'добавь встречу на пятницу' → calendar",
            "'глубокое исследование квантовых компьютеров' → deep_research",
        ]
        return fixed

    async def route(self, message: str, calibration_hint: str | None = None) -> RouteResult:
        """
        Определяет агента для обработки сообщения.
        Трёхуровневая логика: разговор → ключевые слова → LLM.
        """
        stripped = message.strip()

        # Уровень 0а: короткая разговорная реплика → raya мгновенно
        if _CONVERSATIONAL_RE.match(stripped):
            logger.info("🔀 Разговорный матч: '%s' → raya", stripped[:40])
            return RouteResult(
                agent_name="raya",
                confidence=1.0,
                reason="Разговорная реплика",
                used_llm=False,
            )

        # Уровень 0в: голая команда поиска ("найди в интернете", "загугли")
        if _SEARCH_CMD_RE.match(stripped):
            logger.info("🔀 Search-cmd: '%s' → raya", stripped[:40])
            return RouteResult(
                agent_name="raya",
                confidence=1.0,
                reason="Команда поиска — взять тему из контекста",
                used_llm=False,
            )

        # Уровень 0б: явный raya-запрос (время, погода, факты) → raya без LLM
        if _RAYA_RE.search(stripped) and len(stripped.split()) <= 12:
            logger.info("🔀 Raya-матч: '%s' → raya", stripped[:40])
            return RouteResult(
                agent_name="raya",
                confidence=0.95,
                reason="Вопрос-факт или разговорный запрос",
                used_llm=False,
            )

        # Уровень 1: быстрый матч по ключевым словам
        quick = quick_match(message)
        if quick:
            logger.info("🔀 Быстрый матч: '%s' → агент '%s'", message[:50], quick)
            return RouteResult(
                agent_name=quick,
                confidence=0.9,
                reason="Совпадение по ключевым словам",
                used_llm=False,
            )

        # Уровень 2: LLM классификатор
        try:
            prompt = self._build_router_prompt(message, calibration_hint)
            response = await self._llm.ainvoke([HumanMessage(content=prompt)])
            raw = strip_json(str(response.content))
            data = json.loads(raw)

            agent_name = str(data.get("agent", "raya"))
            confidence  = float(data.get("confidence", 0.5))
            reason      = str(data.get("reason", ""))

            # Валидация: агент должен существовать в реестре
            valid_names = {a.name for a in self._agents} | {"raya"}
            if agent_name not in valid_names:
                logger.warning(
                    "Роутер вернул неизвестного агента '%s', fallback → raya",
                    agent_name,
                )
                agent_name = "raya"
                confidence = 0.3
                reason = "Неизвестный агент — fallback"

            logger.info(
                "🔀 LLM роутер: '%s' → агент '%s' (уверенность: %.1f) — %s",
                message[:50], agent_name, confidence, reason,
            )
            return RouteResult(
                agent_name=agent_name,
                confidence=confidence,
                reason=reason,
                used_llm=True,
            )

        except (json.JSONDecodeError, KeyError, ValueError) as e:
            logger.warning("Роутер: ошибка парсинга ответа: %s → fallback raya", e)
        except Exception:
            logger.exception("Роутер: неожиданная ошибка → fallback raya")

        # Fallback — всегда безопасен
        return RouteResult(
            agent_name="raya",
            confidence=0.0,
            reason="Ошибка роутера — fallback",
            used_llm=False,
        )
