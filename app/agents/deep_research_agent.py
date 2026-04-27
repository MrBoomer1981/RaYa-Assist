"""
deep_research_agent.py — агент глубокого исследования.

Использует DeepResearchEngine для многошагового исследования с прогрессом.
Стримит промежуточные обновления в Telegram через edit_message.
Timeout: 120с (глубокое исследование требует времени).
"""
import asyncio
import logging

from app.agents.base_agent import AgentContext, AgentResult, BaseAgent
from app.deep_research import DeepResearchEngine

logger = logging.getLogger(__name__)

# Минимальная длина запроса для глубокого исследования
_MIN_QUERY_LEN = 15


class DeepResearchAgent(BaseAgent):
    agent_name = "deep_research"
    timeout    = 120  # глубокое исследование — долгий процесс

    def _system_prompt(self) -> str:
        return ""  # не используется — синтез внутри DeepResearchEngine

    async def _execute(self, ctx: AgentContext) -> AgentResult:
        query = ctx.message.strip()

        if len(query) < _MIN_QUERY_LEN:
            return AgentResult(
                success=True,
                content="Уточни запрос — для глубокого исследования нужен конкретный вопрос.",
                agent_name=self.agent_name,
            )

        engine   = DeepResearchEngine()
        progress = []

        # Собираем прогресс в список — отдадим через metadata для handlers
        async def _collect(msg: str):
            progress.append(msg)

        # Стримим через async generator
        async for status in engine.research(query, progress_cb=_collect):
            progress.append(status)
            # Небольшая пауза чтобы не перегружать API
            await asyncio.sleep(0.1)

        report = engine.get_report()
        stats  = engine.get_stats()

        logger.info(
            "📚 DeepResearchAgent завершён | %d источников | %s | user_id=%s",
            stats.get("total_sources", 0), stats.get("elapsed", "?"), ctx.user_id,
        )

        return AgentResult(
            success=True,
            content=report,
            agent_name=self.agent_name,
            needs_critic=False,
            metadata={
                "progress":      progress,
                "deep_research": True,
                "stats":         stats,
            },
        )
