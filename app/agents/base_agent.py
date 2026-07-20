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
    user_name: str = "друг"            # ник/имя пользователя — заполняется оркестратором
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

# ── LLM кэш: один объект на модель, не создаём N экземпляров ────────────────
# Максимум 8 моделей в кэше — защита от memory leak при динамической смене моделей
_LLM_CACHE: dict[str, ChatGroq] = {}
_LLM_CACHE_MAX = 8


def _get_llm(model: str) -> ChatGroq:
    """
    Возвращает кэшированный LLM для модели. LRU-like: при переполнении удаляет первый.

    temperature читается заново при КАЖДОМ вызове и применяется даже к уже
    закэшированному клиенту (ChatGroq.temperature — обычное изменяемое поле
    pydantic). Раньше она читалась только в момент СОЗДАНИЯ клиента и
    застревала навсегда — после первого сообщения с моделью клиент уходил
    в кэш, и смена температуры в /settings молча переставала действовать
    до перезапуска процесса.
    """
    import app.settings as _us
    current_temp = _us.get().temperature

    if model not in _LLM_CACHE:
        if len(_LLM_CACHE) >= _LLM_CACHE_MAX:
            # Удаляем самый старый (первый вставленный — dict сохраняет порядок в Python 3.7+)
            oldest = next(iter(_LLM_CACHE))
            del _LLM_CACHE[oldest]
        _LLM_CACHE[model] = ChatGroq(
            api_key=settings.groq_api_key,
            model=model,
            temperature=current_temp,
        )
    else:
        _LLM_CACHE[model].temperature = current_temp

    return _LLM_CACHE[model]


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
        self._llm = _get_llm(model or settings.model_name)
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

        # Единое правило имени — работает для всех агентов
        system = f"{system}\n\nОбращайся к пользователю ТОЛЬКО по имени '{ctx.user_name}'. Никогда не используй другое имя."

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
            _timeout = self.timeout if self.timeout != _DEFAULT_TIMEOUT else settings.agent_timeout
            result = await asyncio.wait_for(
                self._execute(ctx),
                timeout=_timeout,
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
                self.agent_name, _timeout, ctx.user_id,
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
