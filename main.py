import asyncio
import logging
import tempfile
import warnings
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable

warnings.filterwarnings("ignore", message=".*Pydantic V1.*")

from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command
from aiogram.types import Document, Message, PhotoSize, TelegramObject, Voice

from app.config import settings
from app.database import (
    init_db, clear_history, clear_memory, load_memory,
    save_reminder, get_active_reminders, delete_reminder,
)
from app.document_service import SUPPORTED_EXTENSIONS, extract_text
from app.llm_service import LLMService
from app.scheduler_service import SchedulerService
from app.voice_service import VoiceService
from app.vision_service import VisionService

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logging.getLogger("aiogram").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

_MAX_FILE_BYTES = 20 * 1024 * 1024  # 20 МБ


# ── Безопасность ──────────────────────────────────────────────────────────────

class AccessMiddleware(BaseMiddleware):
    """
    Блокирует сообщения от пользователей не из списка ALLOWED_USER_IDS.
    Если список пуст — пропускает всех (режим разработки).
    Чужим не отвечает — тихо игнорирует.
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


# ── Вспомогательные функции ───────────────────────────────────────────────────

def _build_help_text() -> str:
    lines = [
        "🤖 Я твой личный ИИ-ассистент RaYa, Сократ.\n",
        "Что умею:",
        "• Отвечать на вопросы на любом языке",
        "• Помнить факты о тебе между сессиями",
        "• Сохранять историю наших разговоров",
        "• Принимать голосовые сообщения 🎤",
        "• Анализировать фотографии и изображения 🖼️",
        "• Читать и анализировать PDF и Word документы 📄",
        "• Ставить напоминания и задачи ⏰",
    ]
    if settings.search_enabled:
        lines.append("• Искать актуальную информацию в интернете 🔍")
    lines += [
        "\nКоманды:",
        "/reminders — показать активные напоминания",
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
    dp.message.middleware(AccessMiddleware())

    llm = LLMService()
    voice = VoiceService()
    vision = VisionService()
    scheduler = SchedulerService(bot)

    # ── Команды ───────────────────────────────────────────────────────────────

    @dp.message(Command("start"))
    async def cmd_start(message: Message) -> None:
        if not message.from_user:
            return
        await message.answer("Hi Sokrat, я RaYa.")

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
            await message.answer(f"🧠 Вот что я о тебе знаю, Сократ:\n\n{facts_text}")
        else:
            await message.answer("🧠 Пока ничего о тебе не знаю, Сократ.")

    @dp.message(Command("forget"))
    async def cmd_forget(message: Message) -> None:
        if not message.from_user:
            return
        clear_memory(message.from_user.id)
        await message.answer("🗑️ Память удалена. Начинаем заново, Сократ.")

    @dp.message(Command("clear"))
    async def cmd_clear(message: Message) -> None:
        if not message.from_user:
            return
        clear_history(message.from_user.id)
        await message.answer("🗑️ История очищена. Память сохранена, Сократ.")

    @dp.message(Command("reminders"))
    async def cmd_reminders(message: Message) -> None:
        if not message.from_user:
            return
        items = get_active_reminders(message.from_user.id)
        if not items:
            await message.answer("⏰ Активных напоминаний нет, Сократ.")
            return
        lines = ["⏰ Активные напоминания:\n"]
        for rid, text, remind_at in items:
            lines.append(f"[{rid}] {remind_at} — {text}")
        lines.append("\nЧтобы удалить: напиши 'отмени напоминание [номер]'")
        await message.answer("\n".join(lines))

    # ── Голосовые сообщения ───────────────────────────────────────────────────

    @dp.message(lambda m: m.voice is not None)
    async def handle_voice(message: Message) -> None:
        if not message.from_user or not message.voice:
            return

        voice_info: Voice = message.voice
        if voice_info.file_size and voice_info.file_size > _MAX_FILE_BYTES:
            await message.answer("⚠️ Голосовое слишком длинное (макс. 20 МБ).")
            return

        await bot.send_chat_action(message.chat.id, "typing")

        audio_bytes = await _download_bytes(bot, voice_info.file_id)
        if not audio_bytes:
            await message.answer("⚠️ Не удалось скачать аудио.")
            return

        text = await voice.transcribe(audio_bytes)
        if not text:
            await message.answer("⚠️ Не смог распознать голос. Попробуй ещё раз.")
            return

        await message.answer(f"🎤 Распознано: {text}")
        await bot.send_chat_action(message.chat.id, "typing")

        try:
            reply = await llm.chat(message.from_user.id, text)
            await message.answer(reply)
        except Exception:
            logger.exception("Ошибка LLM для голоса user_id=%s", message.from_user.id)
            await message.answer("⚠️ Произошла ошибка.")

    # ── Фотографии ────────────────────────────────────────────────────────────

    @dp.message(lambda m: m.photo is not None)
    async def handle_photo(message: Message) -> None:
        if not message.from_user or not message.photo:
            return

        await bot.send_chat_action(message.chat.id, "typing")

        best_photo: PhotoSize = message.photo[-1]
        if best_photo.file_size and best_photo.file_size > _MAX_FILE_BYTES:
            await message.answer("⚠️ Фото слишком большое (макс. 20 МБ).")
            return

        image_bytes = await _download_bytes(bot, best_photo.file_id)
        if not image_bytes:
            await message.answer("⚠️ Не удалось скачать фото.")
            return

        user_prompt = message.caption or ""
        result = await vision.analyze(image_bytes, user_prompt)
        if not result:
            await message.answer("⚠️ Не смог проанализировать изображение.")
            return

        caption_note = f' (вопрос: "{user_prompt}")' if user_prompt else ""
        llm.save_photo_exchange(
            message.from_user.id,
            f"[Пользователь прислал фото{caption_note}]",
            result,
        )
        await message.answer(f"🖼️ {result}")

    # ── Документы ─────────────────────────────────────────────────────────────

    @dp.message(lambda m: m.document is not None)
    async def handle_document(message: Message) -> None:
        if not message.from_user or not message.document:
            return

        doc: Document = message.document
        file_name = doc.file_name or "документ"
        suffix = Path(file_name).suffix.lower()

        if suffix not in SUPPORTED_EXTENSIONS:
            supported = ", ".join(sorted(SUPPORTED_EXTENSIONS))
            await message.answer(
                f"⚠️ Формат {suffix or 'неизвестный'} не поддерживается.\n"
                f"Принимаю: {supported}"
            )
            return

        if doc.file_size and doc.file_size > _MAX_FILE_BYTES:
            await message.answer("⚠️ Файл слишком большой (макс. 20 МБ).")
            return

        await bot.send_chat_action(message.chat.id, "typing")
        await message.answer(f"📄 Читаю {file_name}...")

        file_bytes = await _download_bytes(bot, doc.file_id)
        if not file_bytes:
            await message.answer("⚠️ Не удалось скачать файл.")
            return

        tmp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
                tmp.write(file_bytes)
                tmp_path = Path(tmp.name)

            try:
                result = extract_text(tmp_path)
            except (ValueError, RuntimeError) as e:
                await message.answer(f"⚠️ {e}")
                return

            if not result.text:
                await message.answer(
                    "⚠️ Не удалось извлечь текст.\n"
                    "Возможно, это сканированный PDF или защищённый файл."
                )
                return

            info_parts = [f"📄 Прочитал: {file_name}"]
            if result.pages:
                info_parts.append(f"Страниц: {result.pages}")
            info_parts.append(f"Символов: {len(result.text):,}")
            if result.truncated:
                info_parts.append("⚠️ Текст обрезан до лимита — анализирую начало.")
            await message.answer("\n".join(info_parts))

            await bot.send_chat_action(message.chat.id, "typing")
            user_question = message.caption or ""

            try:
                reply = await llm.chat_with_document(
                    user_id=message.from_user.id,
                    doc_text=result.text,
                    user_question=user_question,
                    doc_name=file_name,
                )
                await message.answer(reply)
            except Exception:
                logger.exception("Ошибка LLM для документа user_id=%s", message.from_user.id)
                await message.answer("⚠️ Ошибка при анализе. Попробуй ещё раз.")

        finally:
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)

    # ── Текстовые сообщения ───────────────────────────────────────────────────

    @dp.message()
    async def handle_message(message: Message) -> None:
        if not message.text or not message.from_user:
            return

        await bot.send_chat_action(message.chat.id, "typing")

        # Проверяем — не напоминание ли это (параллельно с основным запросом)
        reminder_task = asyncio.create_task(
            llm.extract_reminder(message.from_user.id, message.text)
        )

        try:
            reply = await llm.chat(message.from_user.id, message.text)
            await message.answer(reply)
        except Exception:
            logger.exception("Ошибка user_id=%s", message.from_user.id)
            await message.answer("⚠️ Произошла ошибка. Попробуй ещё раз или напиши /clear")
            reminder_task.cancel()
            return

        # Обрабатываем результат проверки на напоминание
        try:
            reminder_data = await reminder_task
            if reminder_data:
                remind_at = datetime.strptime(
                    reminder_data["remind_at"], "%Y-%m-%d %H:%M"
                )
                rid = save_reminder(
                    message.from_user.id,
                    reminder_data["text"],
                    remind_at,
                )
                await message.answer(
                    f"⏰ Напоминание #{rid} установлено: "
                    f"{reminder_data['text']} — {reminder_data['remind_at']}"
                )
        except Exception:
            logger.debug("Не удалось обработать напоминание для user_id=%s", message.from_user.id)

    # ── Запуск ────────────────────────────────────────────────────────────────

    logger.info(
        "🤖 RaYa запущена | модель: %s | поиск: %s | защита: %s",
        settings.model_name,
        "вкл" if settings.search_enabled else "выкл",
        "вкл" if settings.security_enabled else "выкл",
    )

    scheduler.start()
    try:
        await dp.start_polling(bot, drop_pending_updates=True)
    finally:
        scheduler.stop()
        await bot.session.close()
        logger.info("🛑 RaYa остановлена")


if __name__ == "__main__":
    asyncio.run(main())
