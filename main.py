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


def _build_help_text() -> str:
    """Формирует текст помощи — динамически включает поиск если он включён."""
    lines = [
        "🤖 Я личный ИИ-ассистент RaYa.\n",
        "Что умею:",
        "• Отвечать на вопросы на любом языке",
        "• Помнить факты о тебе между сессиями",
        "• Сохранять историю наших разговоров",
        "• Помогать с текстами, идеями и планами",
    ]
    if settings.search_enabled:
        lines.append("• Искать актуальную информацию в интернете 🔍")

    lines += [
        "\nКоманды:",
        "/memory — показать что знаю о тебе",
        "/forget — удалить память о тебе",
        "/clear — очистить историю разговора",
    ]
    return "\n".join(lines)


async def main() -> None:
    init_db()

    bot = Bot(
        token=settings.telegram_token,
        default=DefaultBotProperties(parse_mode=None),
    )
    dp = Dispatcher()
    llm = LLMService()

    # ── Команды ───────────────────────────────────────────────────────────────

    @dp.message(Command("start"))
    async def cmd_start(message: Message) -> None:
        if not message.from_user:
            return
        name = message.from_user.first_name or "друг"
        search_line = "\n• Искать актуальную информацию 🔍" if settings.search_enabled else ""
        await message.answer(
            f"Привет, {name}! 👋\n\n"
            f"Я твой личный ИИ-ассистент RaYa.\n"
            f"Умею:\n"
            f"• Запоминать тебя и наши разговоры навсегда"
            f"{search_line}\n"
            f"• Помогать с любыми задачами\n\n"
            f"Напиши что-нибудь — начнём! /help для списка команд."
        )

    @dp.message(Command("help"))
    async def cmd_help(message: Message) -> None:
        await message.answer(_build_help_text())

    @dp.message(Command("memory"))
    async def cmd_memory(message: Message) -> None:
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
    async def cmd_forget(message: Message) -> None:
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
            logger.exception("Ошибка при обработке user_id=%s", message.from_user.id)
            await message.answer(
                "⚠️ Произошла ошибка. Попробуй ещё раз или напиши /clear"
            )

    # ── Запуск с graceful shutdown ────────────────────────────────────────────

    logger.info(
        "🤖 Бот запускается | модель: %s | поиск: %s",
        settings.model_name,
        "вкл" if settings.search_enabled else "выкл",
    )
    try:
        await dp.start_polling(bot, drop_pending_updates=True)
    finally:
        await bot.session.close()
        logger.info("🛑 Бот остановлен, соединения закрыты")


if __name__ == "__main__":
    asyncio.run(main())
