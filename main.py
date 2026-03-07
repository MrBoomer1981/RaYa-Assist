import asyncio
import logging
import warnings

warnings.filterwarnings("ignore", message=".*Pydantic V1.*")

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command
from aiogram.types import Message

from app.config import settings
from app.database import init_db, clear_history
from app.llm_service import LLMService

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logging.getLogger("aiogram").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


async def main() -> None:
    # Инициализируем базу данных
    init_db()

    bot = Bot(
        token=settings.telegram_token,
        default=DefaultBotProperties(parse_mode=None),
    )
    dp = Dispatcher()
    llm = LLMService()

    @dp.message(Command("start"))
    async def start(message: Message) -> None:
        name = message.from_user.first_name if message.from_user else "друг"
        await message.answer(
            f"Привет, {name}! 👋\n\n"
            "Я твой ИИ-ассистент RaYa.\n"
            "Команды:\n"
            "/clear — очистить историю разговора\n"
            "/help — помощь"
        )

    @dp.message(Command("help"))
    async def help_command(message: Message) -> None:
        await message.answer(
            "🤖 Я ИИ-ассистент. Вот что я умею:\n\n"
            "• Отвечать на вопросы\n"
            "• Помогать с текстами и идеями\n"
            "• Объяснять сложные темы\n"
            "• Помнить историю даже после перезапуска\n\n"
            "/clear — начать разговор заново"
        )

    @dp.message(Command("clear"))
    async def cmd_clear(message: Message) -> None:
        if message.from_user:
            clear_history(message.from_user.id)
        await message.answer("🗑️ История очищена. Начинаем заново!")

    @dp.message()
    async def handle_message(message: Message) -> None:
        if not message.text or not message.from_user:
            return
        await bot.send_chat_action(message.chat.id, "typing")
        try:
            reply = await llm.chat(message.from_user.id, message.text)
            await message.answer(reply)
        except Exception:
            logger.exception("Ошибка user_id=%s", message.from_user.id)
            await message.answer(
                "⚠️ Произошла ошибка. Попробуй ещё раз или напиши /clear"
            )

    logger.info("🤖 Бот запускается (модель: %s)...", settings.model_name)
    await dp.start_polling(bot, drop_pending_updates=True)


if __name__ == "__main__":
    asyncio.run(main())
