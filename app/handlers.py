"""
handlers.py — все Telegram хендлеры и команды.

Регистрируются через register(dp, bot, services).
Не содержит бизнес-логики — только роутинг и форматирование ответов.
"""
import logging
import tempfile
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any
from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, Message

from app.config import settings
from app.database import (
    clear_history, clear_memory, load_memory,
    save_reminder, get_active_reminders, get_memory_by_category, DB_PATH,
)
from app.document_service import SUPPORTED_EXTENSIONS, extract_text
from app.llm_service import LLMService, ChatResult
from app.voice_service import VoiceService
from app.vision_service import VisionService

logger = logging.getLogger(__name__)

_MAX_FILE_BYTES = 20 * 1024 * 1024  # 20 МБ

from app.utils import RECUR_RU


# ── Вспомогательные ───────────────────────────────────────────────────────────

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
        "• Ставить напоминания (в том числе повторяющиеся) ⏰",
    ]
    if settings.search_enabled:
        lines.append("• Искать и исследовать информацию 🔍")
    lines += [
        "\nКоманды:",
        "/reminders — активные напоминания",
        "/memory    — что знаю о тебе",
        "/forget    — удалить память",
        "/clear     — очистить историю разговора",
        "/debug_time — диагностика времени",
    ]
    return "\n".join(lines)


async def _download_bytes(bot: Bot, file_id: str) -> bytes | None:
    file = await bot.get_file(file_id)
    if not file.file_path:
        return None
    downloaded = await bot.download_file(file.file_path)
    return downloaded.read() if downloaded else None


async def _handle_chat_result(message: Message, result: ChatResult, bot: Bot) -> None:
    """Отправляет ответ: текст, фото от ImageAgent, подтверждение напоминания."""
    if result.agent_name and "image" in result.agent_name:
        image_bytes = (result.metadata or {}).get("image_bytes")
        if image_bytes:
            await message.answer_photo(
                photo=BufferedInputFile(image_bytes, filename="image.jpg"),
                caption=result.reply[:1024] if result.reply else None,
            )
        else:
            await message.answer(result.reply)
        return

    await message.answer(result.reply)

    if result.reminder:
        try:
            remind_str = result.reminder["remind_at"]
            remind_at  = datetime.strptime(remind_str, "%Y-%m-%d %H:%M:%S")
            recurrence = result.reminder.get("recurrence")

            rid = save_reminder(
                message.from_user.id,
                result.reminder["text"],
                remind_at,
                recurrence,
            )
            recur_note = f"\n🔁 {RECUR_RU.get(recurrence, recurrence)}" if recurrence else ""
            await message.answer(
                f"⏰ Записала, Сократ. Напомню: {result.reminder['text']}\n"
                f"Время (UTC): {remind_str}{recur_note} (#{rid})"
            )
        except Exception:
            logger.exception("Ошибка сохранения напоминания")


# ── Регистрация хендлеров ─────────────────────────────────────────────────────

def register(dp: Dispatcher, bot: Bot, llm: LLMService,
             voice: VoiceService, vision: VisionService) -> None:
    """Регистрирует все хендлеры в диспетчере."""

    # ── Команды ───────────────────────────────────────────────────────────────

    @dp.message(Command("start"))
    async def cmd_start(message: Message) -> None:
        if not message.from_user:
            return
        await message.answer("Привет, Сократ. Я RaYa — чем могу помочь?")

    @dp.message(Command("help"))
    async def cmd_help(message: Message) -> None:
        await message.answer(_build_help_text())

    @dp.message(Command("memory"))
    async def cmd_memory(message: Message) -> None:
        if not message.from_user:
            return
        user_id = message.from_user.id
        lines   = []
        facts   = load_memory(user_id)
        if facts:
            lines.append("🧠 Что знаю о тебе:")
            lines.extend(f"  • {f}" for f in facts[:10])
        decisions = get_memory_by_category(user_id, "decisions")
        if decisions:
            lines.append("\n✅ Принятые решения:")
            lines.extend(f"  • {k}: {v}" for k, v in decisions[:8])
        await message.answer("\n".join(lines) if lines else "🧠 Пока ничего о тебе не знаю, Сократ.")

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
        llm._consistency.clear_session(message.from_user.id)
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
        lines.append("\nЧтобы удалить — напиши 'отмени напоминание [номер]'")
        await message.answer("\n".join(lines))

    @dp.message(Command("debug_time"))
    async def cmd_debug_time(message: Message) -> None:
        if not message.from_user:
            return
        import sqlite3
        now_utc = datetime.utcnow()
        conn = sqlite3.connect(str(DB_PATH))
        rows = conn.execute(
            "SELECT id, text, remind_at, done FROM reminders "
            "WHERE user_id = ? ORDER BY id DESC LIMIT 5",
            (message.from_user.id,),
        ).fetchall()
        conn.close()
        lines = [f"🕐 Сейчас UTC: {now_utc.strftime('%Y-%m-%d %H:%M:%S')}\n"]
        if rows:
            lines.append("Последние напоминания:")
            for rid, text, rat, done in rows:
                lines.append(f"[{rid}] {rat} — {text} ({'✅' if done else '⏳'})")
        else:
            lines.append("Напоминаний нет.")
        await message.answer("\n".join(lines))

    # ── Медиа ─────────────────────────────────────────────────────────────────

    @dp.message(lambda m: m.voice is not None)
    async def handle_voice(message: Message) -> None:
        if not message.from_user or not message.voice:
            return
        if message.voice.file_size and message.voice.file_size > _MAX_FILE_BYTES:
            await message.answer("⚠️ Голосовое слишком длинное (макс. 20 МБ).")
            return
        await bot.send_chat_action(message.chat.id, "typing")
        audio = await _download_bytes(bot, message.voice.file_id)
        if not audio:
            await message.answer("⚠️ Не удалось скачать аудио.")
            return
        text = await voice.transcribe(audio)
        if not text:
            await message.answer("⚠️ Не смог распознать голос. Попробуй ещё раз.")
            return
        await message.answer(f"🎤 Распознано: {text}")
        await bot.send_chat_action(message.chat.id, "typing")
        try:
            result = await llm.chat(message.from_user.id, text)
            await _handle_chat_result(message, result, bot)
        except Exception:
            logger.exception("Ошибка LLM voice user_id=%s", message.from_user.id)
            await message.answer("⚠️ Произошла ошибка.")

    @dp.message(lambda m: m.photo is not None)
    async def handle_photo(message: Message) -> None:
        if not message.from_user or not message.photo:
            return
        await bot.send_chat_action(message.chat.id, "typing")
        best = message.photo[-1]
        if best.file_size and best.file_size > _MAX_FILE_BYTES:
            await message.answer("⚠️ Фото слишком большое (макс. 20 МБ).")
            return
        image_bytes = await _download_bytes(bot, best.file_id)
        if not image_bytes:
            await message.answer("⚠️ Не удалось скачать фото.")
            return
        user_prompt = message.caption or ""
        result = await vision.analyze(image_bytes, user_prompt)
        if not result:
            await message.answer("⚠️ Не смог проанализировать изображение.")
            return
        note = f' (вопрос: "{user_prompt}")' if user_prompt else ""
        llm.save_photo_exchange(message.from_user.id, f"[Фото{note}]", result)
        await message.answer(f"🖼️ {result}")

    @dp.message(lambda m: m.document is not None)
    async def handle_document(message: Message) -> None:
        if not message.from_user or not message.document:
            return
        doc      = message.document
        filename = doc.file_name or "документ"
        suffix   = Path(filename).suffix.lower()
        if suffix not in SUPPORTED_EXTENSIONS:
            await message.answer(
                f"⚠️ Формат {suffix or 'неизвестный'} не поддерживается.\n"
                f"Принимаю: {', '.join(sorted(SUPPORTED_EXTENSIONS))}"
            )
            return
        if doc.file_size and doc.file_size > _MAX_FILE_BYTES:
            await message.answer("⚠️ Файл слишком большой (макс. 20 МБ).")
            return
        await bot.send_chat_action(message.chat.id, "typing")
        await message.answer(f"📄 Читаю {filename}...")
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
                doc_result = extract_text(tmp_path)
            except (ValueError, RuntimeError) as e:
                await message.answer(f"⚠️ {e}")
                return
            if not doc_result.text:
                await message.answer("⚠️ Не удалось извлечь текст из файла.")
                return
            info = [f"📄 Прочитал: {filename}"]
            if doc_result.pages:
                info.append(f"Страниц: {doc_result.pages}")
            info.append(f"Символов: {len(doc_result.text):,}")
            if doc_result.truncated:
                info.append("⚠️ Текст обрезан до лимита.")
            await message.answer("\n".join(info))
            await bot.send_chat_action(message.chat.id, "typing")
            try:
                reply = await llm.chat_with_document(
                    user_id=message.from_user.id,
                    doc_text=doc_result.text,
                    user_question=message.caption or "",
                    doc_name=filename,
                )
                await message.answer(reply)
            except Exception:
                logger.exception("Ошибка LLM doc user_id=%s", message.from_user.id)
                await message.answer("⚠️ Ошибка при анализе. Попробуй ещё раз.")
        finally:
            if tmp_path:
                tmp_path.unlink(missing_ok=True)

    # ── Текст ─────────────────────────────────────────────────────────────────

    @dp.message()
    async def handle_message(message: Message) -> None:
        if not message.text or not message.from_user:
            return
        await bot.send_chat_action(message.chat.id, "typing")
        try:
            bridge = await llm.get_resume_phrase(message.from_user.id)
            result = await llm.chat(
                message.from_user.id,
                message.text,
                resume_bridge=bridge,
            )
            await _handle_chat_result(message, result, bot)
        except Exception:
            logger.exception("Ошибка user_id=%s", message.from_user.id)
            await message.answer("⚠️ Произошла ошибка. Попробуй ещё раз или напиши /clear")
