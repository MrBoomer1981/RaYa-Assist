"""
core.py — инициализация и запуск всего приложения.

main.py просто вызывает Core().start().
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties

from app.config import settings
from app.database import init_db

if TYPE_CHECKING:
    from app.llm_service import LLMService
    from app.vision_service import VisionService
    from app.proactive_service import ProactiveService

logger = logging.getLogger(__name__)


@dataclass
class Services:
    bot:       Bot
    llm:       LLMService
    vision:    VisionService
    proactive: ProactiveService


class Core:
    def __init__(self) -> None:
        self._services: Services | None = None

    async def start(self) -> None:
        import app.settings as _S
        _S.get()  # загружаем настройки из файла сразу
        init_db()

        svc = self._init_services()
        self._services = svc

        self._log_startup(svc)
        svc.proactive.start()

        from app.health import start_health_server
        health_runner = await start_health_server(settings.port)

        dp = self._build_dispatcher(svc)

        try:
            await dp.start_polling(svc.bot, drop_pending_updates=True)
        finally:
            svc.proactive.stop()
            await svc.bot.session.close()
            await health_runner.cleanup()
            logger.info("🛑 RaYa остановлена")

    def _init_services(self) -> Services:
        from app.llm_service       import LLMService
        from app.vision_service    import VisionService
        from app.proactive_service import ProactiveService

        bot       = Bot(
            token=settings.telegram_token,
            default=DefaultBotProperties(parse_mode=None),
        )
        llm       = LLMService()
        vision    = VisionService()
        proactive = ProactiveService(bot, llm)

        return Services(bot=bot, llm=llm, vision=vision, proactive=proactive)

    def _build_dispatcher(self, svc: Services) -> Dispatcher:
        from app.middleware import AccessMiddleware
        from app.handlers   import register

        dp = Dispatcher()
        dp.message.middleware(AccessMiddleware())
        dp.callback_query.middleware(AccessMiddleware())
        register(dp, svc.bot, svc.llm, svc.vision)
        return dp

    def _log_startup(self, svc: Services) -> None:
        from app.agents.registry import get_enabled_agents
        agents = [a.name for a in get_enabled_agents()]
        logger.info(
            "🤖 RaYa запущена | модель: %s | поиск: %s | агентов: %d (%s)",
            settings.model_name,
            "вкл" if settings.search_enabled else "выкл",
            len(agents),
            ", ".join(agents),
        )
