"""
middleware.py — защита доступа к боту (single-user).

Пропускает только owner_user_id из .env.
Если owner_user_id = 0 — режим разработки, пропускает всех.
"""
import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

from app.config import settings

logger = logging.getLogger(__name__)


class AccessMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if settings.owner_user_id == 0:
            return await handler(event, data)
        user = data.get("event_from_user")
        if user is None:
            return
        if user.id != settings.owner_user_id:
            logger.warning("🚫 Отклонён user_id=%s (не владелец)", user.id)
            return
        return await handler(event, data)
