"""
router.py — маршрутизатор задач.

Двухуровневая маршрутизация:
1. Быстрый матч по ключевым словам (без LLM, мгновенно)
2. LLM классификатор (только если ключевые слова не дали однозначного ответа)

Возвращает имя агента который должен обработать сообщение.
"""
import json
import logging
from dataclasses import dataclass

from langchain_core.messages import HumanMessage
from langchain_groq import ChatGroq

from app.agents.registry import (
    AgentInfo,
    get_routable_agents,
    quick_match,
)
from app.config import settings

logger = logging.getLogger(__name__)

# Лёгкая быстрая модель для роутера — не тратим тяжёлую модель на классификацию
_ROUTER_MODEL = "llama-3.1-8b-instant"


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
        """Формирует промпт для LLM роутера."""
        agents_desc = "\n".join(
            f'- "{a.name}": {a.description}'
            for a in self._agents
        )
        return (
            f"Сообщение пользователя: {message}\n\n"
            f"Доступные агенты:\n{agents_desc}\n\n"
            "Выбери одного агента который лучше всего подходит для этого сообщения.\n"
            "Если ни один не подходит явно — выбери 'raya'.\n\n"
            "ЖЁСТКИЕ ПРАВИЛА:\n"
            "- агент 'morning' НИКОГДА не выбирать в ответ на сообщение пользователя\n"
            "- вопросы про погоду, новости, курсы → агент 'raya'\n"
            "- агент 'morning' запускается только автоматически, не вручную\n\n"
            "Верни ТОЛЬКО JSON:\n"
            '{"agent": "<имя>", "confidence": <0.0-1.0>, "reason": "<одно предложение>"}\n\n'
            "Только JSON, без пояснений."
        )

    async def route(self, message: str, calibration_hint: str | None = None) -> RouteResult:
        """
        Определяет агента для обработки сообщения.
        Двухуровневая логика: ключевые слова → LLM.
        """
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
            raw = (
                str(response.content)
                .strip()
                .replace("```json", "")
                .replace("```", "")
                .strip()
            )
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
