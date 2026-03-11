import asyncio
import logging
from datetime import datetime

from aiogram import Bot

from app.database import get_due_reminders, mark_reminder_done, reschedule_reminder

logger = logging.getLogger(__name__)

_CHECK_INTERVAL = 60

_RECURRENCE_RU = {
    "daily":   "каждый день",
    "weekly":  "каждую неделю",
    "weekday": "по будням",
    "monthly": "каждый месяц",
}


class SchedulerService:
    """
    Фоновый планировщик напоминаний.
    Каждые 60 секунд проверяет БД и отправляет сообщения.
    Повторяющиеся напоминания автоматически пересоздаются.
    """

    def __init__(self, bot: Bot) -> None:
        self._bot = bot
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())
        logger.info("⏰ Планировщик запущен (интервал: %ds)", _CHECK_INTERVAL)

    def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            logger.info("⏰ Планировщик остановлен")

    async def _run(self) -> None:
        while True:
            try:
                await self._check_reminders()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Ошибка в планировщике")
            await asyncio.sleep(_CHECK_INTERVAL)

    async def _check_reminders(self) -> None:
        now = datetime.utcnow()
        logger.debug("⏰ Проверка: %s UTC", now.strftime("%Y-%m-%d %H:%M:%S"))

        due = get_due_reminders(now)
        if not due:
            return

        logger.info("⏰ Напоминаний к отправке: %d", len(due))

        for reminder_id, user_id, text, recurrence in due:
            try:
                # Формируем текст с пометкой о повторении
                suffix = ""
                if recurrence and recurrence in _RECURRENCE_RU:
                    suffix = f"\n🔁 {_RECURRENCE_RU[recurrence]}"

                await self._bot.send_message(
                    chat_id=user_id,
                    text=f"⏰ Напоминание, Сократ: {text}{suffix}",
                )

                # Повторяющееся — пересоздаём следующее
                if recurrence:
                    rescheduled = reschedule_reminder(reminder_id)
                    if rescheduled:
                        logger.info(
                            "🔁 Напоминание #%d пересоздано (%s)",
                            reminder_id, recurrence,
                        )
                    else:
                        mark_reminder_done(reminder_id)
                else:
                    mark_reminder_done(reminder_id)

                logger.info(
                    "✅ Напоминание #%d отправлено user_id=%s", reminder_id, user_id
                )

            except Exception:
                logger.exception(
                    "Не удалось отправить напоминание #%d user_id=%s",
                    reminder_id, user_id,
                )
