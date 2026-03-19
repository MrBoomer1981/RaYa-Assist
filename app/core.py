"""
core.py — инициализация и владение всеми сервисами приложения.

Единственная точка где создаются сервисы.
main.py просто вызывает Core().start() — больше ничего не знает.

Добавить новый сервис: описать в _Services, создать в _init_services().
"""
import asyncio
import logging
import os
from dataclasses import dataclass

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties

from app.config import settings
from app.database import init_db

logger = logging.getLogger(__name__)


@dataclass
class Services:
    """Все сервисы приложения в одном месте."""
    bot:       "Bot"
    llm:       "LLMService"
    voice:     "VoiceService"
    vision:    "VisionService"
    proactive: "ProactiveService"


class Core:
    """
    Инициализирует, запускает и останавливает всё приложение.

    Использование:
        core = Core()
        await core.start()   # блокирует до остановки
    """

    def __init__(self) -> None:
        self._services: Services | None = None

    # ── Публичный API ─────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Запускает всё приложение."""
        init_db()

        svc = self._init_services()
        self._services = svc

        self._log_startup(svc)
        svc.proactive.start()
        # Синхронизируем БД задач с Obsidian при старте
        asyncio.create_task(self._sync_tasks(svc))

        dp = self._build_dispatcher(svc)

        web_server = self._build_web_server(svc.llm)

        webdav_runner = await self._start_webdav()
        try:
            await asyncio.gather(
                dp.start_polling(svc.bot, drop_pending_updates=True),
                web_server.serve(),
            )
        finally:
            svc.proactive.stop()
            await svc.bot.session.close()
            if webdav_runner:
                await webdav_runner.cleanup()
            logger.info("🛑 RaYa остановлена")

    # ── Инициализация ─────────────────────────────────────────────────────────

    def _init_services(self) -> Services:
        from app.llm_service       import LLMService
        from app.voice_service     import VoiceService
        from app.vision_service    import VisionService
        from app.proactive_service import ProactiveService

        bot       = Bot(
            token=settings.telegram_token,
            default=DefaultBotProperties(parse_mode=None),
        )
        llm       = LLMService()
        voice     = VoiceService()
        vision    = VisionService()
        proactive = ProactiveService(bot, llm)

        return Services(
            bot=bot, llm=llm, voice=voice,
            vision=vision,
 proactive=proactive,
        )

    def _build_dispatcher(self, svc: Services) -> Dispatcher:
        from app.middleware import AccessMiddleware
        from app.handlers   import register

        dp = Dispatcher()
        dp.message.middleware(AccessMiddleware())
        register(dp, svc.bot, svc.llm, svc.voice, svc.vision)
        return dp

    def _build_web_server(self, llm: "LLMService"):
        import uvicorn
        from app.web_server import create_app

        web_app    = create_app(llm)
        web_port   = int(os.getenv("PORT", "8000"))
        web_config = uvicorn.Config(
            web_app, host="0.0.0.0", port=web_port, log_level="warning"
        )
        return uvicorn.Server(web_config)

    async def _sync_tasks(self, svc) -> None:
        """Синхронизирует задачи БД с Obsidian vault."""
        try:
            from app.integrations.obsidian import sync_tasks_to_db, vault_available
            if vault_available():
                result = sync_tasks_to_db(settings.telegram_user_id)
                logger.info("🔄 Задачи синхронизированы: %s", result)
        except Exception:
            logger.warning("Sync tasks: ошибка при старте", exc_info=True)

    async def _start_webdav(self):
        """Запускает WebDAV сервер если задан WEBDAV_PASSWORD."""
        try:
            from app.webdav_server import start_webdav_server
            return await start_webdav_server()
        except Exception:
            logger.exception("WebDAV: ошибка запуска (не критично)")
            return None

    def _log_startup(self, svc: Services) -> None:
        from app.feature_flags import status as ff_status
        ff = ff_status()
        disabled = [k for k, v in ff.items() if not v]
        if disabled:
            logger.info("🚩 Feature flags OFF: %s", ", ".join(disabled))
    def _log_startup(self, svc: Services) -> None:
        from app.agents.registry import get_enabled_agents
        agent_names = [a.name for a in get_enabled_agents()]
        logger.info(
            "🤖 RaYa запущена | модель: %s | поиск: %s | агентов: %d (%s)",
            settings.model_name,
            "вкл" if settings.search_enabled else "выкл",
            len(agent_names),
            ", ".join(agent_names),
        )
