"""
critic_agent.py — финальный агент проверки результатов.
Не генерирует контент — только оценивает и улучшает результат других агентов.
Вызывается программно когда AgentResult.needs_critic=True.
"""
import logging

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_groq import ChatGroq

from app.agents.base_agent import AgentContext, AgentResult, BaseAgent
from app.config import settings

logger = logging.getLogger(__name__)

# Критик работает с температурой 0 — холодная голова, никаких фантазий
_CRITIC_MODEL_TEMPERATURE = 0.0

_SYSTEM = """\
Ты критик-редактор в команде RaYa. Проверяешь результаты других агентов.

Твоя задача — найти и исправить:
- Фактические ошибки
- Логические противоречия
- Неполные или вводящие в заблуждение утверждения
- Код который не работает или небезопасен
- Излишнюю самоуверенность без оснований

Формат ответа:
Если результат хороший — верни его без изменений.
Если нашёл проблемы — исправь и добавь в конце: [Исправлено: <что именно]

Никогда не добавляй лишних слов. Только улучшенный результат."""


class CriticAgent(BaseAgent):
    agent_name = "critic"
    timeout = 30

    def __init__(self) -> None:
        # Критик использует основную модель но с температурой 0
        self._llm = ChatGroq(
            api_key=settings.groq_api_key,
            model=settings.model_name,
            temperature=_CRITIC_MODEL_TEMPERATURE,
        )

    def _system_prompt(self) -> str:
        return _SYSTEM

    async def review(
        self,
        original_result: AgentResult,
        ctx: AgentContext,
    ) -> AgentResult:
        """
        Проверяет результат другого агента.
        Возвращает улучшенный AgentResult.
        """
        prompt = (
            f"Задача пользователя: {ctx.message}\n\n"
            f"Результат агента '{original_result.agent_name}':\n"
            f"{original_result.content}\n\n"
            "Проверь результат. Исправь если нужно."
        )

        try:
            response = await self._llm.ainvoke([
                SystemMessage(content=_SYSTEM),
                HumanMessage(content=prompt),
            ])
            reviewed_content = str(response.content)

            logger.info(
                "🔍 Критик проверил результат агента '%s'",
                original_result.agent_name,
            )

            return AgentResult(
                success=True,
                content=reviewed_content,
                agent_name=f"{original_result.agent_name}+critic",
                needs_critic=False,
                metadata={
                    **original_result.metadata,
                    "critic_applied": True,
                },
            )

        except Exception:
            logger.exception("Критик: ошибка проверки")
            # При ошибке критика — возвращаем оригинал
            return original_result

    async def _execute(self, ctx: AgentContext) -> AgentResult:
        """Не используется напрямую — только через review()."""
        return AgentResult(
            success=False,
            content="Критик вызывается только через review()",
            agent_name=self.agent_name,
            error="Прямой вызов не поддерживается",
        )
