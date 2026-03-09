"""
proactive_service.py — проактивные сообщения от RaYa.

Что делает:
- 08:00 МСК (05:00 UTC) — утренний дайджест
- Проверяет каждую минуту не пора ли что-то отправить
- В будущем: инициативные сообщения по интересам
"""
import asyncio
import logging
from datetime import datetime, timezone, timedelta

from aiogram import Bot

from app.config import settings

logger = logging.getLogger(__name__)

# Самара = UTC+4 (летом), но используем Москву UTC+3 для стабильности
_MOSCOW_UTC_OFFSET = 3
_DIGEST_HOUR_MSK   = 8   # 08:00 по Москве = 05:00 UTC


class ProactiveService:
    """
    Планировщик проактивных сообщений.
    Запускается параллельно с ботом и планировщиком напоминаний.
    """

    def __init__(self, bot: Bot, llm_service) -> None:
        self._bot = bot
        self._llm = llm_service
        self._task: asyncio.Task | None = None
        self._digest_sent_date: str = ""  # дата последнего дайджеста

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())
        logger.info("🌅 Проактивный сервис запущен (дайджест в %d:00 МСК)", _DIGEST_HOUR_MSK)

    def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()

    async def _run(self) -> None:
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Ошибка в proactive_service")
            await asyncio.sleep(60)

    async def _tick(self) -> None:
        now_utc = datetime.utcnow()
        now_msk_hour = (now_utc.hour + _MOSCOW_UTC_OFFSET) % 24
        today_str    = now_utc.strftime("%Y-%m-%d")

        # Отправляем дайджест один раз в день в нужный час
        if now_msk_hour == _DIGEST_HOUR_MSK and self._digest_sent_date != today_str:
            self._digest_sent_date = today_str
            await self._send_morning_digest()

    async def _send_morning_digest(self) -> None:
        """Генерирует и отправляет утренний дайджест."""
        try:
            logger.info("🌅 Генерируем утренний дайджест...")
            user_id = settings.telegram_user_id

            from app.agents.morning_agent import MorningAgent
            from app.agents.base_agent import AgentContext
            from app.database import load_history, load_memory

            agent = MorningAgent()
            ctx = AgentContext(
                user_id=user_id,
                message="утренний дайджест",
                history=load_history(user_id, limit=5),
                memory_facts=load_memory(user_id),
                search_results="",
            )

            result = await agent.run(ctx)

            if result.success and result.content:
                await self._bot.send_message(
                    chat_id=user_id,
                    text=f"🌅 *Доброе утро, Сократ*\n\n{result.content}",
                    parse_mode="Markdown",
                )
                # Сохраняем в историю
                from app.database import save_messages
                save_messages(user_id, "[утренний дайджест]", result.content)
                logger.info("✅ Утренний дайджест отправлен")
            else:
                logger.warning("Дайджест вернул пустой результат")

        except Exception:
            logger.exception("Ошибка отправки дайджеста")
