"""
base_agent.py — базовый класс для всех агентов системы.

Каждый агент наследует BaseAgent и реализует метод _execute().
Общая логика (логирование, обработка ошибок, таймаут) — здесь.
"""
import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from langchain_groq import ChatGroq
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage

from app.config import settings

logger = logging.getLogger(__name__)

def _ms(start: float) -> int:
    """Миллисекунды с момента start."""
    return int((time.monotonic() - start) * 1000)


# Максимальное время выполнения одного агента (секунды)
_DEFAULT_TIMEOUT = 30


@dataclass
class AgentContext:
    """
    Контекст задачи — передаётся в каждый агент.
    Содержит всё необходимое для выполнения задачи.
    """
    user_id: int
    message: str                        # исходное сообщение пользователя
    history: list[BaseMessage] = field(default_factory=list)
    memory_facts: list[str] = field(default_factory=list)
    search_results: str = ""            # результаты поиска если были
    extra: dict[str, Any] = field(default_factory=dict)  # доп. данные агента


@dataclass
class AgentResult:
    """
    Результат работы агента.
    Всегда возвращается — даже при ошибке (success=False).
    """
    success: bool
    content: str                        # текст ответа пользователю
    agent_name: str                     # кто выполнил
    elapsed_ms: int = 0                 # время выполнения
    needs_critic: bool = False          # нужна ли проверка критиком
    metadata: dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None        # текст ошибки если success=False


def strip_history(history: list, limit: int = 0) -> list:
    """
    Конвертирует историю BaseMessage → [HumanMessage, AIMessage].
    limit > 0 — берёт последние N сообщений.
    Используется агентами которым нужна история без системных сообщений.
    """
    from langchain_core.messages import HumanMessage, AIMessage
    items = history[-limit:] if limit > 0 else history
    result = []
    for msg in items:
        cls = msg.__class__.__name__
        if cls == "HumanMessage":
            result.append(HumanMessage(content=msg.content))
        elif cls == "AIMessage":
            result.append(AIMessage(content=msg.content))
    return result

class BaseAgent:
    """
    Базовый класс для всех агентов.

    Наследники реализуют:
      _system_prompt() → str          — системный промпт агента
      _execute(ctx)    → AgentResult  — основная логика

    Всё остальное (таймаут, логирование, fallback) — здесь.
    """

    # Переопределяется в наследниках
    agent_name: str = "base"
    timeout: int = _DEFAULT_TIMEOUT

    def __init__(self, model: Optional[str] = None) -> None:
        self._llm = ChatGroq(
            api_key=settings.groq_api_key,
            model=model or settings.model_name,
            temperature=settings.temperature,
        )
        logger.debug("Агент '%s' инициализирован (модель: %s)",
                     self.agent_name, model or settings.model_name)

    def _system_prompt(self) -> str:
        """Системный промпт агента. Переопределяется в наследниках."""
        return settings.system_prompt

    def _build_messages(
        self,
        ctx: AgentContext,
        user_content: Optional[str] = None,
    ) -> list[BaseMessage]:
        """
        Собирает список сообщений для LLM.
        user_content — если нужно переопределить ctx.message.
        """
        system = self._system_prompt()

        # Добавляем факты о пользователе в системный промпт
        if ctx.memory_facts:
            facts = "\n".join(f"- {f}" for f in ctx.memory_facts)
            system = f"{system}\n\nЧто известно о пользователе:\n{facts}"

        # Добавляем результаты поиска если есть
        content = user_content or ctx.message
        if ctx.search_results:
            content = (
                f"{content}\n\n"
                f"[Актуальная информация из поиска:]\n{ctx.search_results}"
            )

        return [
            SystemMessage(content=system),
            *ctx.history,
            HumanMessage(content=content),
        ]

    async def _execute(self, ctx: AgentContext) -> AgentResult:
        """Основная логика агента. ДОЛЖНА быть переопределена в наследниках."""
        raise NotImplementedError(f"Агент '{self.agent_name}' не реализован")

    async def run(self, ctx: AgentContext) -> AgentResult:
        """
        Публичный метод запуска агента.
        Оборачивает _execute() в таймаут и обработку ошибок.
        """
        start = time.monotonic()
        logger.info("▶️  Агент '%s' запущен | user_id=%s", self.agent_name, ctx.user_id)

        try:
            result = await asyncio.wait_for(
                self._execute(ctx),
                timeout=self.timeout,
            )
            result.elapsed_ms = _ms(start)
            logger.info(
                "✅ Агент '%s' завершён за %dмс | user_id=%s",
                self.agent_name, result.elapsed_ms, ctx.user_id,
            )
            return result

        except asyncio.TimeoutError:
            elapsed = _ms(start)
            logger.error(
                "⏱️  Агент '%s' timeout (%dс) | user_id=%s",
                self.agent_name, self.timeout, ctx.user_id,
            )
            return AgentResult(
                success=False,
                content="⚠️ Агент не ответил вовремя. Попробуй ещё раз.",
                agent_name=self.agent_name,
                elapsed_ms=elapsed,
                error=f"Timeout после {self.timeout}с",
            )

        except Exception as e:
            elapsed = _ms(start)
            logger.exception(
                "❌ Агент '%s' ошибка | user_id=%s", self.agent_name, ctx.user_id
            )
            return AgentResult(
                success=False,
                content="⚠️ Произошла ошибка. Попробуй ещё раз.",
                agent_name=self.agent_name,
                elapsed_ms=elapsed,
                error=str(e),
            )
