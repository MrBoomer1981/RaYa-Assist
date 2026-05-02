"""
handlers.py — все Telegram хендлеры и команды.

Регистрируются через register(dp, bot, services).
Не содержит бизнес-логики — только роутинг и форматирование ответов.

Оптимизировано для 25+ одновременных пользователей:
  - Rate limiting: не более 1 запроса в 3 сек на пользователя
  - Глобальный семафор: не более 20 параллельных LLM-запросов
  - typing-индикатор не блокирует очередь
"""
import asyncio
import logging
import tempfile
from datetime import datetime
from pathlib import Path
from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, Message, CallbackQuery
from app.settings_ui import handle_settings_callback, kb_main, text_main

from app.config import settings
from app.database import (
    clear_history, clear_memory, load_memory,
    save_reminder, get_active_reminders, get_memory_by_category,
    upsert_user, get_user_name,
)
from app.document_service import SUPPORTED_EXTENSIONS, extract_text
from app.llm_service import LLMService, ChatResult
from app.voice_service import VoiceService
from app.vision_service import VisionService

logger = logging.getLogger(__name__)

_MAX_FILE_BYTES = 20 * 1024 * 1024  # 20 МБ

from app.utils import RECUR_RU

# ── Rate limiting ─────────────────────────────────────────────────────────────
# Защита от спама: 1 запрос / 3 сек на пользователя
_RATE_LIMIT_SEC = 3.0
_last_request: dict[int, float] = {}

# Глобальный семафор: не более 20 параллельных LLM-запросов
# При 25+ пользователях это предотвращает перегрузку Groq API
_LLM_SEMAPHORE = asyncio.Semaphore(settings.max_concurrent)


def _is_rate_limited(user_id: int) -> bool:
    """Возвращает True если пользователь пишет слишком часто."""
    import time
    now = time.monotonic()
    last = _last_request.get(user_id, 0.0)
    if now - last < _RATE_LIMIT_SEC:
        return True
    _last_request[user_id] = now
    return False


# ── Вспомогательные ───────────────────────────────────────────────────────────

def _build_help_text() -> str:
    lines = [
        "🤖 Я твой личный ИИ-ассистент RaYa.\n",
        "Что умею:",
        "• Отвечать на вопросы на любом языке",
        "• Помнить факты о тебе между сессиями",
        "• Сохранять историю наших разговоров",
        "• Принимать голосовые сообщения 🎤",
        "• Анализировать фотографии и изображения 🖼️",
        "• Читать и анализировать PDF и Word документы 📄",
        "• Ставить напоминания (в том числе повторяющиеся) ⏰",
        "• Управлять задачами и дедлайнами 📋",
    ]
    if settings.search_enabled:
        lines.append("• Искать и исследовать информацию 🔍")
    lines += [
        "\nКоманды:",
        "/settings  — персональные настройки",
        "/reminders — активные напоминания",
        "/memory    — что знаю о тебе",
        "/forget    — удалить память",
        "/stats     — личная статистика за неделю",
        "/clear     — очистить историю разговора",
        "/help      — это сообщение",
    ]
    return "\n".join(lines)


_DEEP_RESEARCH_TRIGGERS = frozenset({
    "глубокое исследование", "deep research", "подробный анализ",
    "исследуй глубоко", "детальный отчёт", "полный анализ",
    "всё о", "расскажи подробно всё", "подготовь отчёт",
    "аналитика по", "детально изучи", "развёрнутый анализ",
})


def _is_deep_research_request(message: str) -> bool:
    msg = message.lower()
    return any(t in msg for t in _DEEP_RESEARCH_TRIGGERS)


async def _keep_typing(bot: Bot, chat_id: int, stop: asyncio.Event) -> None:
    """Периодически отправляет typing пока stop не установлен."""
    while not stop.is_set():
        try:
            await bot.send_chat_action(chat_id, "typing")
        except Exception:
            break
        await asyncio.sleep(4)


async def _download_bytes(bot: Bot, file_id: str) -> bytes | None:
    file = await bot.get_file(file_id)
    if not file.file_path:
        return None
    downloaded = await bot.download_file(file.file_path)
    return downloaded.read() if downloaded else None


async def _send_deep_research(message: Message, llm: "LLMService", bot: Bot) -> None:
    """
    Запускает глубокое исследование с живым прогрессом через edit_message.
    Показывает каждый шаг как редактирование одного сообщения.
    """
    from app.deep_research import DeepResearchEngine

    # Отправляем начальное сообщение — будем его редактировать
    status_msg = await message.answer("🔬 Начинаю глубокое исследование...")
    progress_lines: list[str] = []

    async def _update_progress(line: str):
        progress_lines.append(line)
        # Показываем последние 6 строк чтобы не перегружать
        display = "\n".join(progress_lines[-6:])
        try:
            await bot.edit_message_text(
                text=f"🔬 *Исследование в процессе...*\n\n{display}",
                chat_id=message.chat.id,
                message_id=status_msg.message_id,
                parse_mode="Markdown",
            )
        except Exception:
            pass  # edit может упасть если сообщение не изменилось

    engine = DeepResearchEngine()
    async for status in engine.research(message.text or "", progress_cb=_update_progress):
        await _update_progress(status)

    report = engine.get_report()

    # Удаляем прогресс-сообщение и отправляем финальный отчёт
    try:
        await bot.delete_message(message.chat.id, status_msg.message_id)
    except Exception:
        pass

    # Telegram ограничивает сообщение 4096 символами — режем если нужно
    if len(report) <= 4096:
        await message.answer(report, parse_mode="Markdown")
    else:
        # Отправляем частями по ~3800 символов с разбивкой по абзацам
        chunks = _split_report(report, max_len=3800)
        for i, chunk in enumerate(chunks):
            prefix = f"📚 *Отчёт (часть {i+1}/{len(chunks)})*\n\n" if len(chunks) > 1 else ""
            await message.answer(prefix + chunk, parse_mode="Markdown")


def _split_report(text: str, max_len: int = 3800) -> list[str]:
    """Разбивает длинный текст по абзацам не превышая max_len."""
    chunks: list[str] = []
    current = ""
    for para in text.split("\n\n"):
        if len(current) + len(para) + 2 <= max_len:
            current += ("\n\n" if current else "") + para
        else:
            if current:
                chunks.append(current)
            current = para
    if current:
        chunks.append(current)
    return chunks or [text[:max_len]]


async def _handle_chat_result(
    message: Message, result: ChatResult, bot: Bot,
    tts=None,
) -> None:
    """Отправляет ответ: текст, фото от ImageAgent, TTS, напоминание."""
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

    # ── TTS: если включён голосовой ответ для этого пользователя ─────────────
    if tts and tts.enabled and message.from_user:
        try:
            from app.user_settings import get_settings
            user_cfg = get_settings(message.from_user.id)
            if getattr(user_cfg, "voice_response", False) and result.reply:
                audio = await tts.synthesize(result.reply, is_voice=True)
                if audio:
                    from aiogram.types import BufferedInputFile as _BIF
                    await bot.send_voice(
                        chat_id=message.chat.id,
                        voice=_BIF(audio, filename="reply.mp3"),
                    )
        except Exception:
            logger.debug("TTS send failed — silent skip")

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
                f"⏰ Записала. Напомню: {result.reminder['text']}\n"
                f"Время (UTC): {remind_str}{recur_note} (#{rid})"
            )
        except Exception:
            logger.exception("Ошибка сохранения напоминания")


# ── Регистрация хендлеров ─────────────────────────────────────────────────────

def _build_stats(user_id: int) -> str:
    """Собирает статистику за неделю — один SQL-запрос вместо пяти соединений."""
    from datetime import datetime, timedelta
    from app.database import _conn

    now      = datetime.utcnow()
    week_ago = (now - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
    lines    = ["📊 **Твоя статистика за неделю**\n"]

    _MOOD_EMOJI = {
        "радость": "😊", "вдохновение": "🔥", "спокойствие": "😌",
        "гордость": "💪", "грусть": "😔", "усталость": "😴",
        "тревога": "😰", "злость": "😤", "скука": "😑", "нейтрально": "😐",
    }

    try:
        with _conn() as con:
            msg_count = con.execute(
                "SELECT COUNT(*) FROM history WHERE user_id=? AND created_at>=?",
                (user_id, week_ago),
            ).fetchone()[0]

            done_week = con.execute(
                "SELECT COUNT(*) FROM tasks WHERE user_id=? AND done=1 AND created_at>=?",
                (user_id, week_ago),
            ).fetchone()[0]

            active_tasks = con.execute(
                "SELECT COUNT(*) FROM tasks WHERE user_id=? AND done=0",
                (user_id,),
            ).fetchone()[0]

            diary_count = con.execute(
                "SELECT COUNT(*) FROM diary WHERE user_id=? AND created_at>=?",
                (user_id, week_ago),
            ).fetchone()[0]

            mood_rows = con.execute(
                "SELECT mood FROM mood_log WHERE user_id=? AND created_at>=? ORDER BY created_at DESC",
                (user_id, week_ago),
            ).fetchall()

            events_count = con.execute(
                "SELECT COUNT(*) FROM events WHERE user_id=? AND date>=date('now') AND date<=date('now','+7 days')",
                (user_id,),
            ).fetchone()[0]

            topic_rows = con.execute(
                "SELECT topic FROM interaction_memory WHERE user_id=? ORDER BY frequency DESC, last_seen DESC LIMIT 3",
                (user_id,),
            ).fetchall()

    except Exception:
        return "📊 Не удалось получить статистику."

    if msg_count:
        lines.append(f"💬 Сообщений: **{msg_count}**")
    if done_week or active_tasks:
        lines.append(f"✅ Задач закрыто: **{done_week}** | В работе: **{active_tasks}**")
    if diary_count:
        lines.append(f"📓 Записей в дневнике: **{diary_count}**")
    if mood_rows:
        moods     = [r[0] for r in mood_rows]
        top_mood  = max(set(moods), key=moods.count)
        lines.append(f"🧠 Настроение: {_MOOD_EMOJI.get(top_mood, '🙂')} **{top_mood}**")
    if topic_rows:
        lines.append(f"🗣️ Частые темы: {', '.join(r[0] for r in topic_rows)}")
    if events_count:
        lines.append(f"📅 Событий на неделе: **{events_count}**")

    if len(lines) == 1:
        lines.append("_Пока нет данных — пообщайся со мной несколько дней_")

    return "\n".join(lines)


def register(dp: Dispatcher, bot: Bot, llm: LLMService,
             voice: VoiceService, vision: VisionService, tts: None = None) -> None:
    """Регистрирует все хендлеры в диспетчере."""

    # ── Команды ───────────────────────────────────────────────────────────────

    @dp.message(Command("start"))
    async def cmd_start(message: Message) -> None:
        if not message.from_user:
            return
        u = message.from_user
        upsert_user(u.id, u.first_name or "", u.last_name or "", u.username or "")
        name = get_user_name(u.id)
        await message.answer(f"Привет, {name}! Я RaYa — чем могу помочь?")

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
            lines.extend(f"  • {k}: {v}" for k, v in list(decisions.items())[:8])
        await message.answer("\n".join(lines) if lines else "🧠 Пока ничего о тебе не знаю.")

    @dp.message(Command("forget"))
    async def cmd_forget(message: Message) -> None:
        if not message.from_user:
            return
        clear_memory(message.from_user.id)
        await message.answer("🗑️ Память удалена. Начинаем заново.")

    @dp.message(Command("clear"))
    async def cmd_clear(message: Message) -> None:
        if not message.from_user:
            return
        clear_history(message.from_user.id)
        llm._consistency.clear_session(message.from_user.id)
        await message.answer("🗑️ История очищена. Память сохранена.")

    @dp.message(Command("reminders"))
    async def cmd_reminders(message: Message) -> None:
        if not message.from_user:
            return
        items = get_active_reminders(message.from_user.id)
        if not items:
            await message.answer("⏰ Активных напоминаний нет.")
            return
        lines = ["⏰ Активные напоминания:\n"]
        for rid, text, remind_at in items:
            lines.append(f"[{rid}] {remind_at} — {text}")
        lines.append("\nЧтобы удалить — напиши 'отмени напоминание [номер]'")
        await message.answer("\n".join(lines))

    # ── Медиа ─────────────────────────────────────────────────────────────────

    @dp.message(Command("stats"))
    async def cmd_stats(message: Message) -> None:
        if not message.from_user:
            return
        uid = message.from_user.id
        upsert_user(uid, message.from_user.first_name or "",
                    message.from_user.last_name or "", message.from_user.username or "")
        await message.answer(_build_stats(uid), parse_mode="Markdown")

    @dp.message(Command("settings"))
    async def cmd_settings(message: Message) -> None:
        if not message.from_user:
            return
        uid = message.from_user.id
        await message.answer(
            text_main(uid),
            reply_markup=kb_main(uid),
            parse_mode="Markdown",
        )

    @dp.callback_query(lambda c: c.data and c.data.startswith("s:"))
    async def on_settings_callback(callback: CallbackQuery) -> None:
        if not callback.from_user:
            return
        await handle_settings_callback(callback, callback.from_user.id)

    @dp.message(lambda m: m.voice is not None)
    async def handle_voice(message: Message) -> None:
        if not message.from_user or not message.voice:
            return
        u = message.from_user
        upsert_user(u.id, u.first_name or "", u.last_name or "", u.username or "")

        if _is_rate_limited(u.id):
            await message.answer("⏳ Не так быстро — подожди секунду.")
            return

        if message.voice.file_size and message.voice.file_size > _MAX_FILE_BYTES:
            await message.answer("⚠️ Голосовое слишком длинное (макс. 20 МБ).")
            return

        stop_typing = asyncio.Event()
        typing_task = asyncio.create_task(_keep_typing(bot, message.chat.id, stop_typing))
        try:
            audio = await _download_bytes(bot, message.voice.file_id)
            if not audio:
                await message.answer("⚠️ Не удалось скачать аудио.")
                return
            text = await voice.transcribe(audio)
            if not text:
                await message.answer("⚠️ Не смог распознать голос. Попробуй ещё раз.")
                return
            await message.answer(f"🎤 Распознано: {text}")
            async with _LLM_SEMAPHORE:
                result = await llm.chat(u.id, text)
            await _handle_chat_result(message, result, bot, tts=tts)
        except Exception:
            logger.exception("Ошибка LLM voice user_id=%s", u.id)
            await message.answer("⚠️ Произошла ошибка.")
        finally:
            stop_typing.set()
            typing_task.cancel()

    @dp.message(lambda m: m.photo is not None)
    async def handle_photo(message: Message) -> None:
        if not message.from_user or not message.photo:
            return
        u = message.from_user
        upsert_user(u.id, u.first_name or "", u.last_name or "", u.username or "")

        if _is_rate_limited(u.id):
            await message.answer("⏳ Не так быстро — подожди секунду.")
            return

        stop_typing = asyncio.Event()
        typing_task = asyncio.create_task(_keep_typing(bot, message.chat.id, stop_typing))
        try:
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
            llm.save_photo_exchange(u.id, f"[Фото{note}]", result)
            await message.answer(f"🖼️ {result}")
        except Exception:
            logger.exception("Ошибка vision user_id=%s", u.id)
            await message.answer("⚠️ Произошла ошибка при анализе фото.")
        finally:
            stop_typing.set()
            typing_task.cancel()

    @dp.message(lambda m: m.document is not None)
    async def handle_document(message: Message) -> None:
        if not message.from_user or not message.document:
            return
        u = message.from_user
        upsert_user(u.id, u.first_name or "", u.last_name or "", u.username or "")

        if _is_rate_limited(u.id):
            await message.answer("⏳ Не так быстро — подожди секунду.")
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

        stop_typing = asyncio.Event()
        typing_task = asyncio.create_task(_keep_typing(bot, message.chat.id, stop_typing))
        await message.answer(f"📄 Читаю {filename}...")
        tmp_path: Path | None = None
        try:
            file_bytes = await _download_bytes(bot, doc.file_id)
            if not file_bytes:
                await message.answer("⚠️ Не удалось скачать файл.")
                return
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
            async with _LLM_SEMAPHORE:
                reply = await llm.chat_with_document(
                    user_id=u.id,
                    doc_text=doc_result.text,
                    user_question=message.caption or "",
                    doc_name=filename,
                )
            await message.answer(reply)
        except Exception:
            logger.exception("Ошибка LLM doc user_id=%s", u.id)
            await message.answer("⚠️ Ошибка при анализе. Попробуй ещё раз.")
        finally:
            stop_typing.set()
            typing_task.cancel()
            if tmp_path:
                tmp_path.unlink(missing_ok=True)

    # ── Текст ─────────────────────────────────────────────────────────────────

    @dp.message()
    async def handle_message(message: Message) -> None:
        if not message.text or not message.from_user:
            return
        u = message.from_user
        upsert_user(u.id, u.first_name or "", u.last_name or "", u.username or "")
        # Сбрасываем prompt cache если имя изменилось в Telegram
        try:
            from app.database import invalidate_user_name_cache
            invalidate_user_name_cache(u.id)
        except Exception:
            pass

        # Rate limiting — защита от спама
        if _is_rate_limited(u.id):
            await message.answer("⏳ Не так быстро — подожди секунду.")
            return

        stop_typing = asyncio.Event()
        typing_task = asyncio.create_task(_keep_typing(bot, message.chat.id, stop_typing))
        try:
            # Deep research: запускаем отдельный flow с live-прогрессом
            if _is_deep_research_request(message.text or ""):
                await _send_deep_research(message, llm, bot)
                return

            bridge = await llm.get_resume_phrase(u.id)
            async with _LLM_SEMAPHORE:
                result = await llm.chat(
                    u.id,
                    message.text,
                    resume_bridge=bridge,
                )
            await _handle_chat_result(message, result, bot, tts=tts)
        except Exception:
            logger.exception("Ошибка user_id=%s", u.id)
            await message.answer("⚠️ Произошла ошибка. Попробуй ещё раз или напиши /clear")
        finally:
            stop_typing.set()
            typing_task.cancel()
