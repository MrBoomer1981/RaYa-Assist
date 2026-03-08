"""
orchestrator.py — главный оркестратор системы RaYa.

Принимает задачу → роутер определяет агента → агент выполняет →
критик проверяет если нужно → возвращает результат.

RaYa остаётся точкой входа — она решает всё.
"""
import asyncio
import logging
from typing import Optional

from app.agents.base_agent import AgentContext, AgentResult
from app.agents.registry import get_agent
from app.agents.router import RouterAgent, RouteResult
from app.config import settings
from app.database import load_history, load_memory

logger = logging.getLogger(__name__)


class Orchestrator:
    """
    Центральный координатор агентов.
    Инициализируется один раз при старте бота.
    Агенты создаются лениво — только при первом обращении.
    """

    def __init__(self) -> None:
        self._router = RouterAgent()
        self._agents: dict = {}   # имя → экземпляр агента (lazy init)
        self._critic: Optional[object] = None
        logger.info("🧠 Оркестратор инициализирован")

    def _get_agent(self, name: str):
        """Возвращает агента по имени. Создаёт при первом обращении."""
        if name not in self._agents:
            self._agents[name] = _create_agent(name)
            logger.info("🔧 Агент '%s' создан", name)
        return self._agents[name]

    def _get_critic(self):
        """Возвращает критика. Создаёт при первом обращении."""
        if self._critic is None:
            from app.agents.critic_agent import CriticAgent
            self._critic = CriticAgent()
            logger.info("🔧 Критик создан")
        return self._critic

    async def run(
        self,
        user_id: int,
        message: str,
        search_results: str = "",
    ) -> AgentResult:
        """
        Главный метод — принимает сообщение, возвращает результат.
        Полный цикл: роутинг → выполнение → критика (если нужно).
        """
        # Загружаем контекст пользователя
        history  = load_history(user_id, limit=settings.max_history)
        memory   = load_memory(user_id)

        ctx = AgentContext(
            user_id=user_id,
            message=message,
            history=history,
            memory_facts=memory,
            search_results=search_results,
        )

        # Роутинг
        route: RouteResult = await self._router.route(message)
        logger.info(
            "🔀 Роутинг: '%s' → агент '%s' (уверенность: %.1f, LLM: %s)",
            message[:60], route.agent_name, route.confidence, route.used_llm,
        )

        # Получаем агента
        agent = self._get_agent(route.agent_name)
        if agent is None:
            logger.warning("Агент '%s' не найден → fallback raya", route.agent_name)
            agent = self._get_agent("raya")

        # Выполняем
        result: AgentResult = await agent.run(ctx)

        # Критик — если агент запросил и он доступен
        if result.success and result.needs_critic:
            logger.info("🔍 Запускаем критика для агента '%s'", result.agent_name)
            try:
                critic = self._get_critic()
                result = await critic.review(result, ctx)
            except Exception:
                logger.exception("Критик упал — возвращаем оригинал")

        return result


def _create_agent(name: str):
    """Фабрика агентов по имени из реестра."""
    # Проверяем что агент есть в реестре
    info = get_agent(name)
    if info is None or not info.enabled:
        logger.warning("Агент '%s' не найден в реестре → None", name)
        return None

    # Импортируем нужный класс
    try:
        if name == "raya":
            from app.agents.raya_agent import RayaAgent
            return RayaAgent()
        elif name == "code":
            from app.agents.code_agent import CodeAgent
            return CodeAgent()
        elif name == "image":
            from app.agents.image_agent import ImageAgent
            return ImageAgent()
        elif name == "diary":
            from app.agents.diary_agent import DiaryAgent
            return DiaryAgent()
        elif name == "science":
            from app.agents.science_agent import ScienceAgent
            return ScienceAgent()
        elif name == "critic":
            from app.agents.critic_agent import CriticAgent
            return CriticAgent()
        else:
            logger.warning("Неизвестный агент '%s'", name)
            return None
    except Exception:
        logger.exception("Ошибка создания агента '%s'", name)
        return None
