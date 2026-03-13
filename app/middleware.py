"""
middleware.py — Telegram middleware.

Вынесен из main.py чтобы core.py оставался чистым.
"""
import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

from app.config import settings

logger = logging.getLogger(__name__)


class AccessMiddleware(BaseMiddleware):
    """
    Блокирует сообщения не из ALLOWED_USER_IDS.
    Если список пуст — пропускает всех (режим разработки).
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not settings.security_enabled:
            return await handler(event, data)
        user = data.get("event_from_user")
        if user is None:
            return
        if user.id not in settings.allowed_ids:
            logger.warning("🚫 Доступ запрещён: user_id=%s", user.id)
            return
        return await handler(event, data)
