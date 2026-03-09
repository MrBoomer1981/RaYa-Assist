"""
orchestrator.py — координатор агентов.
Роутинг → выполнение → критика → результат.
"""
import logging
from typing import Optional

from app.agents.base_agent import AgentContext, AgentResult
from app.agents.critic_agent import CriticAgent
from app.agents.registry import get_agent
from app.agents.router import RouterAgent, RouteResult
from app.config import settings
from app.database import load_history, load_memory

logger = logging.getLogger(__name__)


def _create_agent(name: str):
    """Фабрика агентов по имени."""
    info = get_agent(name)
    if info is None or not info.enabled:
        logger.warning("Агент '%s' не найден в реестре", name)
        return None
    try:
        match name:
            case "raya":
                from app.agents.raya_agent import RayaAgent
                return RayaAgent()
            case "code":
                from app.agents.code_agent import CodeAgent
                return CodeAgent()
            case "image":
                from app.agents.image_agent import ImageAgent
                return ImageAgent()
            case "diary":
                from app.agents.diary_agent import DiaryAgent
                return DiaryAgent()
            case "science":
                from app.agents.science_agent import ScienceAgent
                return ScienceAgent()
            case "todo":
                from app.agents.todo_agent import TodoAgent
                return TodoAgent()
            case "morning":
                from app.agents.morning_agent import MorningAgent
                return MorningAgent()
            case "text":
                from app.agents.text_agent import TextAgent
                return TextAgent()
            case "ideas":
                from app.agents.ideas_agent import IdeasAgent
                return IdeasAgent()
            case "planning":
                from app.agents.planning_agent import PlanningAgent
                return PlanningAgent()
            case "critic":
                return CriticAgent()
            case _:
                logger.warning("Неизвестный агент '%s'", name)
                return None
    except Exception:
        logger.exception("Ошибка создания агента '%s'", name)
        return None


class Orchestrator:
    """
    Координатор агентов.
    Агенты создаются лениво — только при первом обращении.
    """

    def __init__(self) -> None:
        self._router = RouterAgent()
        self._agents: dict = {}
        self._critic: Optional[CriticAgent] = None
        logger.info("🧠 Оркестратор инициализирован")

    def _get_agent(self, name: str):
        if name not in self._agents:
            self._agents[name] = _create_agent(name)
            logger.info("🔧 Агент '%s' создан", name)
        return self._agents[name]

    def _get_critic(self) -> CriticAgent:
        if self._critic is None:
            self._critic = CriticAgent()
            logger.info("🔧 Критик создан")
        return self._critic

    async def run(
        self,
        user_id: int,
        message: str,
        search_results: str = "",
    ) -> AgentResult:
        """Полный цикл: роутинг → агент → критик (если нужно)."""
        ctx = AgentContext(
            user_id=user_id,
            message=message,
            history=load_history(user_id, limit=settings.max_history),
            memory_facts=load_memory(user_id),
            search_results=search_results,
        )

        route: RouteResult = await self._router.route(message)
        logger.info(
            "🔀 '%s' → агент '%s' (уверенность: %.1f, LLM: %s)",
            message[:60], route.agent_name, route.confidence, route.used_llm,
        )

        agent = self._get_agent(route.agent_name) or self._get_agent("raya")
        result: AgentResult = await agent.run(ctx)

        if result.success and result.needs_critic:
            logger.info("🔍 Критик проверяет '%s'", result.agent_name)
            try:
                result = await self._get_critic().review(result, ctx)
            except Exception:
                logger.exception("Критик упал — возвращаем оригинал")

        return result
