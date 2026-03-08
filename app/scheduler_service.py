import asyncio
import logging
from datetime import datetime, timezone

from aiogram import Bot

from app.database import get_due_reminders, mark_reminder_done

logger = logging.getLogger(__name__)

# Интервал проверки напоминаний в секундах
_CHECK_INTERVAL = 60


class SchedulerService:
    """
    Фоновый планировщик напоминаний.
    Каждые 60 секунд проверяет БД и отправляет сообщения в Telegram.
    """

    def __init__(self, bot: Bot) -> None:
        self._bot = bot
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        """Запускает фоновую задачу планировщика."""
        self._task = asyncio.create_task(self._run())
        logger.info("⏰ Планировщик запущен (интервал: %ds)", _CHECK_INTERVAL)

    def stop(self) -> None:
        """Останавливает планировщик."""
        if self._task and not self._task.done():
            self._task.cancel()
            logger.info("⏰ Планировщик остановлен")

    async def _run(self) -> None:
        """Основной цикл планировщика."""
        while True:
            try:
                await self._check_reminders()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Ошибка в планировщике")
            await asyncio.sleep(_CHECK_INTERVAL)

    async def _check_reminders(self) -> None:
        """Проверяет и отправляет просроченные напоминания."""
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        due = get_due_reminders(now)

        if not due:
            return

        logger.info("⏰ Найдено напоминаний: %d", len(due))

        for reminder_id, user_id, text in due:
            try:
                await self._bot.send_message(
                    chat_id=user_id,
                    text=f"⏰ Напоминание: {text}",
                )
                mark_reminder_done(reminder_id)
                logger.info("✅ Напоминание #%d отправлено user_id=%s", reminder_id, user_id)
            except Exception:
                logger.exception(
                    "Не удалось отправить напоминание #%d user_id=%s",
                    reminder_id, user_id,
                )
