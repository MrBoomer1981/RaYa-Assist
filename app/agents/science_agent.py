"""
science_agent.py — агент проверки научных данных.
Использует поиск для актуальной информации.
Всегда указывает источники и степень достоверности.
"""
import logging
from typing import Optional

from app.agents.base_agent import AgentContext, AgentResult, BaseAgent
from app.config import settings

logger = logging.getLogger(__name__)

_SYSTEM = """\
Ты научный аналитик в команде RaYa. Специализация: верификация фактов и данных.

Правила:
- Всегда разделяй факты и интерпретации
- Указывай степень достоверности: [Подтверждено] / [Вероятно] / [Спорно] / [Опровергнуто]
- Ссылайся на источники если они есть в контексте поиска
- Признавай когда данных недостаточно
- Не делай выводов сверх имеющихся данных
- Указывай дату данных если информация может устареть

Обращайся к пользователю только "Сократ"."""


class ScienceAgent(BaseAgent):
    agent_name = "science"
    timeout = 40

    def __init__(self) -> None:
        super().__init__()
        self._search: Optional[object] = None
        if settings.search_enabled:
            from app.search_service import SearchService
            self._search = SearchService()
            logger.info("🔬 Science Agent: поиск включён")

    def _system_prompt(self) -> str:
        return _SYSTEM

    async def _execute(self, ctx: AgentContext) -> AgentResult:
        # Science агент всегда использует поиск если доступен
        search_results = ctx.search_results
        if not search_results and self._search:
            try:
                search_results = await self._search.search(ctx.message)  # type: ignore[attr-defined]
                logger.info("🔬 Science: поиск выполнен для '%s'", ctx.message[:50])
            except Exception:
                logger.warning("Science: поиск недоступен")

        import dataclasses
        enriched_ctx = dataclasses.replace(ctx, search_results=search_results)
        messages = self._build_messages(enriched_ctx)
        response = await self._llm.ainvoke(messages)
        content = str(response.content)

        return AgentResult(
            success=True,
            content=content,
            agent_name=self.agent_name,
            needs_critic=True,  # научные данные тоже проверяем
            metadata={"search_used": bool(search_results)},
        )
