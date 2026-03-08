import asyncio
import logging
import warnings
from typing import Any, Awaitable, Callable

warnings.filterwarnings("ignore", message=".*Pydantic V1.*")

from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command
from aiogram.types import Message, PhotoSize, TelegramObject, Voice

from app.config import settings
from app.database import init_db, clear_history, clear_memory, load_memory
from app.llm_service import LLMService
from app.voice_service import VoiceService
from app.vision_service import VisionService

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logging.getLogger("aiogram").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

_MAX_VOICE_BYTES = 20 * 1024 * 1024
_MAX_PHOTO_BYTES = 20 * 1024 * 1024


# ── Безопасность ──────────────────────────────────────────────────────────────

class AccessMiddleware(BaseMiddleware):
    """
    Блокирует сообщения от пользователей не из списка ALLOWED_USER_IDS.
    Если список пуст — пропускает всех (режим разработки).
    Чужим не сообщает что бот существует — тихо игнорирует.
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
            return  # тихо игнорируем

        return await handler(event, data)


# ── Вспомогательные функции ───────────────────────────────────────────────────

def _build_help_text() -> str:
    lines = [
        "🤖 Я личный ИИ-ассистент RaYa.\n",
        "Что умею:",
        "• Отвечать на вопросы на любом языке",
        "• Помнить факты о тебе между сессиями",
        "• Сохранять историю наших разговоров",
        "• Принимать голосовые сообщения 🎤",
        "• Анализировать фотографии и изображения 🖼️",
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


async def _download_bytes(bot: Bot, file_id: str) -> bytes | None:
    """Скачивает файл по file_id и возвращает байты."""
    file = await bot.get_file(file_id)
    if not file.file_path:
        return None
    downloaded = await bot.download_file(file.file_path)
    if downloaded is None:
        return None
    return downloaded.read()


# ── Основной цикл ─────────────────────────────────────────────────────────────

async def main() -> None:
    init_db()

    bot = Bot(
        token=settings.telegram_token,
        default=DefaultBotProperties(parse_mode=None),
    )
    dp = Dispatcher()

    # Подключаем middleware безопасности глобально
    dp.message.middleware(AccessMiddleware())

    llm = LLMService()
    voice = VoiceService()
    vision = VisionService()

    # ── Команды ───────────────────────────────────────────────────────────────

    @dp.message(Command("start"))
    async def cmd_start(message: Message) -> None:
        if not message.from_user:
            return
        name = message.from_user.first_name or "друг"
        search_line = (
            "\n• Искать актуальную информацию 🔍"
            if settings.search_enabled else ""
        )
        await message.answer(
            f"Привет, {name}! 👋\n\n"
            f"Я твой личный ИИ-ассистент RaYa.\n"
            f"Умею:\n"
            f"• Запоминать тебя и наши разговоры навсегда\n"
            f"• Принимать голосовые сообщения 🎤\n"
            f"• Анализировать фотографии 🖼️"
            f"{search_line}\n"
            f"• Помогать с любыми задачами\n\n"
            f"Напиши, надиктуй или пришли фото — начнём!\n/help для команд."
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

    # ── Голосовые сообщения ───────────────────────────────────────────────────

    @dp.message(lambda m: m.voice is not None)
    async def handle_voice(message: Message) -> None:
        if not message.from_user or not message.voice:
            return

        voice_info: Voice = message.voice
        if voice_info.file_size and voice_info.file_size > _MAX_VOICE_BYTES:
            await message.answer("⚠️ Голосовое сообщение слишком длинное (макс. 20 МБ).")
            return

        await bot.send_chat_action(message.chat.id, "typing")

        audio_bytes = await _download_bytes(bot, voice_info.file_id)
        if not audio_bytes:
            await message.answer("⚠️ Не удалось скачать аудио. Попробуй ещё раз.")
            return

        text = await voice.transcribe(audio_bytes)
        if not text:
            await message.answer(
                "⚠️ Не смог распознать голос.\n"
                "Попробуй говорить чётче или напиши текстом."
            )
            return

        await message.answer(f"🎤 Распознано: {text}")
        await bot.send_chat_action(message.chat.id, "typing")

        try:
            reply = await llm.chat(message.from_user.id, text)
            await message.answer(reply)
        except Exception:
            logger.exception("Ошибка LLM для голоса user_id=%s", message.from_user.id)
            await message.answer("⚠️ Произошла ошибка. Попробуй ещё раз.")

    # ── Фотографии ────────────────────────────────────────────────────────────

    @dp.message(lambda m: m.photo is not None)
    async def handle_photo(message: Message) -> None:
        if not message.from_user or not message.photo:
            return

        await bot.send_chat_action(message.chat.id, "typing")

        best_photo: PhotoSize = message.photo[-1]
        if best_photo.file_size and best_photo.file_size > _MAX_PHOTO_BYTES:
            await message.answer("⚠️ Фото слишком большое (макс. 20 МБ).")
            return

        image_bytes = await _download_bytes(bot, best_photo.file_id)
        if not image_bytes:
            await message.answer("⚠️ Не удалось скачать фото. Попробуй ещё раз.")
            return

        user_prompt = message.caption or ""
        result = await vision.analyze(image_bytes, user_prompt)
        if not result:
            await message.answer(
                "⚠️ Не смог проанализировать изображение. Попробуй ещё раз."
            )
            return

        caption_note = f' (вопрос: "{user_prompt}")' if user_prompt else ""
        llm.save_photo_exchange(
            message.from_user.id,
            f"[Пользователь прислал фото{caption_note}]",
            result,
        )

        await message.answer(f"🖼️ {result}")

    # ── Текстовые сообщения ───────────────────────────────────────────────────

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

    # ── Запуск ────────────────────────────────────────────────────────────────

    logger.info(
        "🤖 Бот запущен | модель: %s | поиск: %s | голос: вкл | фото: вкл | защита: %s",
        settings.model_name,
        "вкл" if settings.search_enabled else "выкл",
        "вкл" if settings.security_enabled else "выкл (открытый доступ)",
    )
    try:
        await dp.start_polling(bot, drop_pending_updates=True)
    finally:
        await bot.session.close()
        logger.info("🛑 Бот остановлен")


if __name__ == "__main__":
    asyncio.run(main())
