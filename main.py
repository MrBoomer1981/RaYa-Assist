import asyncio
import logging
import warnings

warnings.filterwarnings("ignore", message=".*Pydantic V1.*")

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command
from aiogram.types import Message

from app.config import settings
from app.database import init_db, clear_history, clear_memory, load_memory
from app.llm_service import LLMService

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logging.getLogger("aiogram").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


async def main() -> None:
    init_db()

    bot = Bot(
        token=settings.telegram_token,
        default=DefaultBotProperties(parse_mode=None),
    )
    dp = Dispatcher()
    llm = LLMService()

    @dp.message(Command("start"))
    async def start(message: Message) -> None:
        if not message.from_user:
            return
        name = message.from_user.first_name
        await message.answer(
            f"Привет, {name}! 👋\n\n"
            "Я твой личный ИИ-ассистент RaYa.\n"
            "Запоминаю тебя и наши разговоры навсегда!\n\n"
            "Команды:\n"
            "/memory — что я о тебе помню\n"
            "/forget — удалить память обо мне\n"
            "/clear — очистить историю разговора\n"
            "/help — помощь"
        )

    @dp.message(Command("help"))
    async def help_command(message: Message) -> None:
        await message.answer(
            "🤖 Я личный ИИ-ассистент RaYa.\n\n"
            "Что умею:\n"
            "• Отвечать на вопросы на любом языке\n"
            "• Помнить факты о тебе между сессиями\n"
            "• Сохранять историю наших разговоров\n"
            "• Помогать с текстами, идеями и планами\n\n"
            "/memory — показать что знаю о тебе\n"
            "/forget — удалить память о тебе\n"
            "/clear — очистить историю разговора"
        )

    @dp.message(Command("memory"))
    async def memory_command(message: Message) -> None:
        if not message.from_user:
            return
        facts = load_memory(message.from_user.id)
        if facts:
            facts_text = "\n".join(f"• {f}" for f in facts)
            await message.answer(f"🧠 Вот что я о тебе знаю:\n\n{facts_text}")
        else:
            await message.answer(
                "🧠 Пока ничего о тебе не знаю.\n"
                "Расскажи немного о себе — запомню!"
            )

    @dp.message(Command("forget"))
    async def forget_command(message: Message) -> None:
        if not message.from_user:
            return
        clear_memory(message.from_user.id)
        await message.answer("🗑️ Память о тебе удалена. Начинаем знакомство заново!")

    @dp.message(Command("clear"))
    async def cmd_clear(message: Message) -> None:
        if not message.from_user:
            return
        clear_history(message.from_user.id)
        await message.answer("🗑️ История разговора очищена. Память о тебе сохранена!")

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
