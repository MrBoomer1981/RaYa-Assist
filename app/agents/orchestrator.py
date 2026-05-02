"""
orchestrator.py — координатор агентов.
Роутинг → выполнение → критика → результат.
"""
import logging
from typing import Optional

from app.agents.base_agent import AgentContext, AgentResult
from app.agents.critic_agent import CriticAgent
from app.agents.registry import create_agent
from app.agents.router import RouterAgent, RouteResult
from app.config import settings
from app.database import load_history, load_memory, get_user_name

logger = logging.getLogger(__name__)

# ── Фабрика агентов через реестр ──────────────────────────────────────────────
# Чтобы добавить нового агента — добавь запись в registry.py (module + class_name).
# Этот файл трогать не нужно.

def _create_agent(name: str):
    """Создаёт агента через реестр. Fallback на CriticAgent для 'critic'."""
    if name == "critic":
        return CriticAgent()
    agent = create_agent(name)
    if agent is None:
        logger.warning("Агент '%s' не найден или отключён", name)
    return agent


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
        is_voice: bool = False,
        extra: dict | None = None,
    ) -> AgentResult:
        """Полный цикл: роутинг → агент → критик (если нужно)."""
        combined_extra = {"is_voice": is_voice}
        if extra:
            combined_extra.update(extra)

        ctx = AgentContext(
            user_id=user_id,
            message=message,
            user_name=get_user_name(user_id),   # кэшировано LRU-256, дёшево
            history=load_history(user_id, limit=settings.max_history),
            memory_facts=load_memory(user_id),
            search_results=search_results,
            extra=combined_extra,
        )

        calibration_hint = extra.get("calibration_hint") if extra else None
        route: RouteResult = await self._router.route(message, calibration_hint)
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

        # Чистим ответ от URL, ссылок, служебных тегов — всегда, для всех агентов
        if result.success and result.content:
            from app.utils import clean_reply
            result.content = clean_reply(result.content)

        return result
