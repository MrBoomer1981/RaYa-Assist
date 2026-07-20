"""
handlers.py — Telegram хендлеры (single-user версия).

Регистрирует: команды, текст, фото (vision), документы, голос,
deep research (выбор режима + live-прогресс).
"""
import asyncio
import logging
import tempfile
import time
from datetime import datetime
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.filters import Command, CommandObject
from aiogram.types import Message, CallbackQuery

from app.config import settings
from app.database import (
    clear_history, clear_memory, save_reminder, get_active_reminders, upsert_user, get_user_name,
    set_digest_subscription, is_digest_subscribed,
)
from app.document_service import SUPPORTED_EXTENSIONS, extract_text
from app.llm_service import LLMService, ChatResult
from app.utils import RECUR_RU, utcnow
from app.vision_service import VisionService

logger = logging.getLogger(__name__)

_MAX_FILE_BYTES = 20 * 1024 * 1024  # 20 МБ


# ── Вспомогательные ───────────────────────────────────────────────────────────

def _build_help_text() -> str:
    lines = [
        "🤖 Я твой личный ИИ-ассистент RaYa.\n",
        "Что умею:",
        "• Отвечать на вопросы и вести диалог",
        "• Помнить факты о тебе между сессиями",
        "• Анализировать фотографии и изображения 🖼️",
        "• Читать и анализировать PDF и Word документы 📄",
        "• Ставить напоминания (в том числе повторяющиеся) ⏰",
        "• Управлять задачами и дедлайнами 📋",
        "• Вести дневник 📓",
        "• Управлять расписанием 📅",
    ]
    if settings.search_enabled:
        lines.append("• Глубоко исследовать темы в интернете 🔬")
    lines += [
        "\nКоманды:",
        "/reminders — активные напоминания",
        "/memory    — что знаю о тебе",
        "/forget    — удалить память",
        "/stats     — статистика за неделю",
        "/clear     — очистить историю разговора",
        "/settings  — настройки (время дайджеста, модули, модель)",
        "/digest    — подписаться/отписаться от утреннего дайджеста",
        "/schedule  — показать сохранённое расписание",
    ]
    if settings.search_enabled:
        lines.append("/deeper <тема> — глубокое исследование темы (DEEper)")
    lines += [
        "🎤 Голосовые сообщения поддерживаются",
        "/help      — это сообщение",
    ]
    return "\n".join(lines)


async def _keep_typing(bot: Bot, chat_id: int, stop: asyncio.Event) -> None:
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


_SCHEDULE_KEYWORDS = frozenset({
    "расписание", "schedule", "занятия", "уроки", "пары", "лекции",
    "рабочий план", "план на неделю", "план недели", "распорядок",
    "сохрани расписание", "запомни расписание", "это моё расписание",
})


def _is_schedule_photo(caption: str) -> bool:
    """Определяет по подписи — это фото расписания."""
    if not caption:
        return False
    cap = caption.lower()
    return any(kw in cap for kw in _SCHEDULE_KEYWORDS)


async def _transcribe_voice(bot: Bot, voice) -> str:
    """
    Транскрибирует голосовое сообщение через Groq Whisper.
    Возвращает текст или пустую строку при ошибке.
    """
    import io
    from groq import AsyncGroq
    from app.config import settings

    try:
        # Скачиваем ogg файл
        file_info = await bot.get_file(voice.file_id)
        downloaded = await bot.download_file(file_info.file_path)
        if not downloaded:
            return ""

        audio_bytes = downloaded.read()
        client = AsyncGroq(api_key=settings.groq_api_key)

        # Groq Whisper принимает file-like объект
        audio_file = io.BytesIO(audio_bytes)
        audio_file.name = "voice.ogg"

        transcription = await client.audio.transcriptions.create(
            file=audio_file,
            model="whisper-large-v3-turbo",  # быстрая и точная модель
            language="ru",
            response_format="text",
        )
        text = str(transcription).strip()
        logger.info("🎤 Транскрипция: '%s...'", text[:50])
        return text
    except Exception as e:
        logger.warning("🎤 Ошибка транскрипции: %s", e)
        return ""


# Хранилище тем ожидающих выбора режима: chat_id → topic
_PENDING_RESEARCH: dict[int, str] = {}

# Chat_id → timestamp: ждём от пользователя тему после голого /deeper
_AWAITING_TOPIC: dict[int, float] = {}
_AWAITING_TOPIC_TTL = 300  # 5 минут — после этого голый текст больше не считается темой

# Удержание фоновых asyncio-задач (deep research и т.п.) от сборки мусора.
# asyncio хранит только слабую ссылку на Task — без своей сильной ссылки
# где-то ещё задача может быть собрана GC прямо посреди выполнения, без
# единой ошибки в логах (см. предупреждение в докладе asyncio.create_task).
_BG_TASKS: set = set()


def _consume_awaiting_topic(chat_id: int) -> bool:
    """
    Если для этого чата ждём тему после голого /deeper — снимает ожидание
    и возвращает True (при условии что не протухло). Иначе False.
    """
    ts = _AWAITING_TOPIC.pop(chat_id, None)
    if ts is None:
        return False
    return (time.monotonic() - ts) < _AWAITING_TOPIC_TTL


_MODES = {
    "simple": ("🟢 Быстрый",     "~2 мин · 5 запросов · базовый обзор"),
    "deep":   ("🔵 Углублённый", "~5 мин · 15 запросов · детальный анализ"),
    "study":  ("🟣 Изучение",    "~8 мин · 20 запросов · полное погружение"),
}


async def _send_deep_research(message: Message, bot: Bot, topic: str) -> None:
    """
    Шаг 1: показывает inline-кнопки выбора режима для уже известной темы.
    Шаг 2 (после выбора): запускает DEEper с live-прогрессом.
    """
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

    topic = topic.strip()
    if not topic:
        await message.answer("Укажи тему, например: `/deeper квантовые компьютеры`",
                             parse_mode="Markdown")
        return

    # Сохраняем тему и показываем выбор режима
    _PENDING_RESEARCH[message.chat.id] = topic

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"{emoji} {desc}",
            callback_data=f"dr:{mode}",
        )]
        for mode, (emoji, desc) in _MODES.items()
    ] + [[InlineKeyboardButton(text="❌ Отмена", callback_data="dr:cancel")]])

    await message.answer(
        f"🔬 *Тема:* {topic}\n\nВыбери глубину исследования:",
        reply_markup=keyboard,
        parse_mode="Markdown",
    )


async def _run_deep_research(chat_id: int, topic: str, mode: str, bot: Bot,
                              status_msg_id: int) -> None:
    """Запускает DEEper и отправляет отчёт."""
    from app.agents.deep_research_agent import _get_bridge
    from deeper.config import RESEARCH_MODES

    progress_lines: list[str] = []

    async def _update(line: str) -> None:
        progress_lines.append(line)
        display = "\n".join(progress_lines[-6:])
        try:
            mode_label = _MODES.get(mode, (mode, ""))[0]
            await bot.edit_message_text(
                text=f"🔬 *{mode_label} · {topic[:40]}*\n\n{display}",
                chat_id=chat_id,
                message_id=status_msg_id,
                parse_mode="Markdown",
            )
        except Exception:
            pass

    try:
        bridge = _get_bridge()
    except Exception as e:
        await bot.edit_message_text(
            text=f"⚠️ DEEper не запустился: {e}",
            chat_id=chat_id,
            message_id=status_msg_id,
        )
        return

    # Раньше здесь не было НИКАКОГО таймаута — при зависании (например,
    # каскад рейт-лимитов Groq, см. groq_rotator.py) запрос мог висеть
    # неограниченно долго, и пользователь просто никогда не получал ответ,
    # даже сообщения об ошибке. Потолок — из RESEARCH_MODES, свой на режим.
    timeout_sec = RESEARCH_MODES.get(mode, RESEARCH_MODES["deep"]).timeout_sec

    start = time.monotonic()
    try:
        result = await asyncio.wait_for(
            bridge.research(topic=topic, mode=mode, progress_cb=_update),
            timeout=timeout_sec,
        )
    except asyncio.TimeoutError:
        elapsed = round(time.monotonic() - start, 1)
        logger.warning(
            "⏱️ DEEper timeout (%dс, режим=%s, прошло=%.1fс) | chat_id=%s",
            timeout_sec, mode, elapsed, chat_id,
        )
        await bot.edit_message_text(
            text=(
                f"⚠️ Исследование заняло больше {timeout_sec // 60} мин и было остановлено.\n\n"
                f"Попробуй режим попроще ({_MODES.get('simple', ('🟢 Простой',))[0]}) "
                f"или повтори — иногда помогает."
            ),
            chat_id=chat_id,
            message_id=status_msg_id,
        )
        return
    except Exception as e:
        await bot.edit_message_text(
            text=f"⚠️ Ошибка: {e}",
            chat_id=chat_id,
            message_id=status_msg_id,
        )
        return

    elapsed = round(time.monotonic() - start, 1)
    report  = result.get("report", "Отчёт не сформирован.")
    sources = result.get("sources", [])
    res_id  = result.get("id")
    mode_label = _MODES.get(mode, (mode, ""))[0]

    footer = f"\n\n---\n{mode_label} | ⏱ {elapsed}с | 📚 {len(sources)} источников"
    if res_id:
        footer += f" | ID: {res_id}"

    try:
        await bot.delete_message(chat_id, status_msg_id)
    except Exception:
        pass

    chunks = _split_report(report + footer, max_len=3800)
    for i, chunk in enumerate(chunks):
        prefix = f"📚 *Часть {i+1}/{len(chunks)}*\n\n" if len(chunks) > 1 else ""
        await bot.send_message(chat_id, prefix + chunk, parse_mode="Markdown")


def _split_report(text: str, max_len: int = 3800) -> list[str]:
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


async def _handle_chat_result(message: Message, result: ChatResult, bot: Bot) -> None:
    """Отправляет ответ пользователю."""
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
                f"⏰ Записала. Напомню: {result.reminder['text']}\n"
                f"Время (UTC): {remind_str}{recur_note} (#{rid})"
            )
        except Exception:
            logger.exception("Ошибка сохранения напоминания")


# ── Статистика ────────────────────────────────────────────────────────────────

def _build_stats(user_id: int) -> str:
    from datetime import timedelta
    from app.database import _conn

    now      = utcnow()
    week_ago = (now - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
    lines    = ["📊 **Статистика за неделю**\n"]

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
                "SELECT mood FROM mood_log WHERE user_id=? AND created_at>=?",
                (user_id, week_ago),
            ).fetchall()
            events_count = con.execute(
                "SELECT COUNT(*) FROM events WHERE user_id=? AND date>=date('now') AND date<=date('now','+7 days')",
                (user_id,),
            ).fetchone()[0]
            topic_rows = con.execute(
                "SELECT topic FROM interaction_memory WHERE user_id=? ORDER BY frequency DESC LIMIT 3",
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
        moods    = [r[0] for r in mood_rows]
        top_mood = max(set(moods), key=moods.count)
        lines.append(f"🧠 Настроение: {_MOOD_EMOJI.get(top_mood, '🙂')} **{top_mood}**")
    if topic_rows:
        lines.append(f"🗣️ Частые темы: {', '.join(r[0] for r in topic_rows)}")
    if events_count:
        lines.append(f"📅 Событий на неделе: **{events_count}**")

    if len(lines) == 1:
        lines.append("_Пока нет данных_")

    return "\n".join(lines)


# ── Регистрация хендлеров ─────────────────────────────────────────────────────

def register(dp: Dispatcher, bot: Bot, llm: LLMService, vision: VisionService) -> None:
    """Регистрирует все хендлеры в диспетчере."""
    from app.commands.settings_cmd import router as settings_router
    dp.include_router(settings_router)

    @dp.callback_query(lambda c: c.data and c.data.startswith("dr:"))
    async def handle_deeper_mode(callback: CallbackQuery) -> None:
        """Обрабатывает выбор режима DEEper."""
        mode = callback.data.split(":", 1)[1]
        chat_id = callback.message.chat.id

        if mode == "cancel":
            _PENDING_RESEARCH.pop(chat_id, None)
            await callback.message.edit_text("❌ Отменено.")
            await callback.answer()
            return

        topic = _PENDING_RESEARCH.pop(chat_id, None)
        if not topic:
            await callback.answer("Тема не найдена. Отправь запрос заново.")
            return

        mode_label = _MODES.get(mode, (mode, ""))[0]
        await callback.message.edit_text(
            f"🔬 *{mode_label}*\n📌 {topic}\n\nНачинаю исследование...",
            parse_mode="Markdown",
        )
        await callback.answer()

        import asyncio as _asyncio
        _task = _asyncio.create_task(
            _run_deep_research(chat_id, topic, mode, bot, callback.message.message_id)
        )
        _BG_TASKS.add(_task)
        _task.add_done_callback(_BG_TASKS.discard)

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
        from app.services.memory.core import get_core_facts
        from app.services.memory.recall import get_recent, count as ep_count

        lines = []

        # Core Memory
        facts = get_core_facts(user_id, limit=20)
        if facts:
            lines.append("🧠 *Core Memory* (всегда в контексте):")
            by_cat: dict = {}
            for f in facts:
                by_cat.setdefault(f["category"], []).append(
                    f"{f['key']}: {f['value']} [★{f['importance']:.0f}]"
                )
            for cat, items in by_cat.items():
                lines.append(f"\n  *{cat}*")
                lines.extend(f"    • {it}" for it in items[:5])
        else:
            lines.append("🧠 Core Memory пуста.")

        # Recall Memory
        ep_n = ep_count(user_id)
        if ep_n > 0:
            lines.append(f"\n💾 *Recall Memory* — {ep_n} эпизодов:")
            recent = get_recent(user_id, limit=3)
            for ep in recent:
                date = ep["created_at"][:10]
                lines.append(f"  [{date}] {ep['summary'][:120]}")

        await message.answer("\n".join(lines) if lines else "🧠 Пока ничего не знаю.", 
                             parse_mode="Markdown")

    @dp.message(Command("forget"))
    async def cmd_forget(message: Message) -> None:
        if not message.from_user:
            return
        clear_memory(message.from_user.id)
        await message.answer("🗑️ Память удалена.")

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

    @dp.message(Command("stats"))
    async def cmd_stats(message: Message) -> None:
        if not message.from_user:
            return
        uid = message.from_user.id
        upsert_user(uid, message.from_user.first_name or "",
                    message.from_user.last_name or "", message.from_user.username or "")
        await message.answer(_build_stats(uid), parse_mode="Markdown")

    @dp.message(Command("schedule"))
    async def cmd_schedule(message: Message) -> None:
        """Показывает сохранённое расписание."""
        if not message.from_user:
            return
        from app.database import get_schedule_photo
        sched = get_schedule_photo(message.from_user.id)
        if not sched:
            await message.answer(
                "📅 Расписание не сохранено.\n\n"
                "Отправь фото расписания с подписью 'расписание' — запомню."
            )
            return
        updated = sched["updated_at"][:10] if sched["updated_at"] else ""
        text = (
            f"📅 **Твоё расписание** (обновлено {updated})\n\n"
            f"{sched['raw_text'][:3000]}"
        )
        await message.answer(text, parse_mode="Markdown")

    @dp.message(Command("deeper"))
    async def cmd_deeper(message: Message, command: CommandObject) -> None:
        """Запускает DEEper. `/deeper тема` — сразу, голый `/deeper` — спросит тему."""
        if not message.from_user:
            return
        topic = (command.args or "").strip()
        if topic:
            await _send_deep_research(message, bot, topic)
        else:
            _AWAITING_TOPIC[message.chat.id] = time.monotonic()
            await message.answer("🔬 Какую тему исследовать?")

    @dp.message(Command("digest"))
    async def cmd_digest(message: Message) -> None:
        """
        Переключает подписку на утренний дайджест (вкл/выкл).

        Раньше дайджест уходил единственному угаданному "владельцу" —
        теперь это рассылка по подписчикам, и каждый решает сам, получать
        её или нет.
        """
        if not message.from_user:
            return
        u = message.from_user
        upsert_user(u.id, u.first_name or "", u.last_name or "", u.username or "")

        now_subscribed = not is_digest_subscribed(u.id)
        set_digest_subscription(u.id, now_subscribed)

        import app.settings as _us_mod
        digest_time = f"{_us_mod.get().digest_hour:02d}:{_us_mod.get().digest_minute:02d}"
        if now_subscribed:
            await message.answer(
                f"🌅 Подписал на утренний дайджест — жди его в {digest_time} МСК.\n"
                f"Отключить — снова /digest."
            )
        else:
            await message.answer("🔕 Отписал от утреннего дайджеста. Включить обратно — /digest.")

    # ── Медиа ─────────────────────────────────────────────────────────────────

    @dp.message(lambda m: m.photo is not None)
    async def handle_photo(message: Message) -> None:
        if not message.from_user or not message.photo:
            return
        u = message.from_user
        upsert_user(u.id, u.first_name or "", u.last_name or "", u.username or "")
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
            # Определяем — расписание или обычное фото
            is_schedule = _is_schedule_photo(user_prompt)

            if is_schedule:
                # Специальный промпт для извлечения расписания
                schedule_prompt = (
                    "Это фото расписания. Извлеки всё расписание структурированно:\n"
                    "- Для каждого дня недели перечисли все занятия/задачи\n"
                    "- Укажи время если видно\n"
                    "- Сохрани все детали точно как на фото\n"
                    "Формат: День недели → Время: Название"
                )
                result = await vision.analyze(image_bytes, schedule_prompt)
                if not result:
                    await message.answer("⚠️ Не смог прочитать расписание.")
                    return

                # Сохраняем расписание
                from app.database import save_schedule_photo
                summary_prompt = f"Дай краткое резюме расписания в 1-2 предложениях:\n{result[:500]}"
                summary_result = await vision.analyze(image_bytes, summary_prompt)
                save_schedule_photo(u.id, result, summary_result or "")
                llm.save_photo_exchange(u.id, "[Расписание сохранено]", result)

                await message.answer(
                    f"📅 Расписание сохранено! Буду показывать его каждое утро.\n\n"
                    f"**Что извлекла:**\n{result[:800]}"
                    + ("..." if len(result) > 800 else ""),
                    parse_mode="Markdown"
                )
            else:
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

    @dp.message(lambda m: m.voice is not None)
    async def handle_voice(message: Message) -> None:
        """Голосовое сообщение → транскрипция → обычный текстовый pipeline."""
        if not message.from_user or not message.voice:
            return
        u = message.from_user
        upsert_user(u.id, u.first_name or "", u.last_name or "", u.username or "")

        # Показываем что обрабатываем
        stop_typing = asyncio.Event()
        typing_task = asyncio.create_task(_keep_typing(bot, message.chat.id, stop_typing))

        try:
            # Транскрибируем
            text = await _transcribe_voice(bot, message.voice)

            if not text:
                await message.answer("🎤 Не смог разобрать голосовое. Попробуй ещё раз.")
                return

            # Показываем что услышали
            await message.answer(f"🎤 _Услышала:_ {text}", parse_mode="Markdown")

            # Обрабатываем как обычный текст
            from app.commands.settings_cmd import handle_pending_input
            setting_reply = handle_pending_input(u.id, text)
            if setting_reply is not None:
                await message.answer(setting_reply, parse_mode="Markdown")
                return

            if _consume_awaiting_topic(message.chat.id):
                stop_typing.set()
                typing_task.cancel()
                await _send_deep_research(message, bot, text)
                return

            bridge = await llm.get_resume_phrase(u.id)
            result = await llm.chat(u.id, text, resume_bridge=bridge)
            await _handle_chat_result(message, result, bot)

        except Exception:
            logger.exception("Ошибка voice user_id=%s", u.id)
            await message.answer("⚠️ Произошла ошибка. Попробуй ещё раз.")
        finally:
            stop_typing.set()
            typing_task.cancel()

    # ── Текст ─────────────────────────────────────────────────────────────────

    @dp.message()
    async def handle_message(message: Message) -> None:
        if not message.text or not message.from_user:
            return
        u = message.from_user
        upsert_user(u.id, u.first_name or "", u.last_name or "", u.username or "")
        try:
            from app.database import invalidate_user_name_cache
            invalidate_user_name_cache(u.id)
        except Exception:
            pass  # cache invalidation — некритично

        stop_typing = asyncio.Event()
        typing_task = asyncio.create_task(_keep_typing(bot, message.chat.id, stop_typing))
        try:
            # Ждём тему после голого /deeper — это следующее сообщение и есть тема
            if _consume_awaiting_topic(message.chat.id):
                stop_typing.set()
                typing_task.cancel()
                await _send_deep_research(message, bot, message.text)
                return

            bridge = await llm.get_resume_phrase(u.id)
            result = await llm.chat(u.id, message.text, resume_bridge=bridge)
            await _handle_chat_result(message, result, bot)
        except Exception:
            logger.exception("Ошибка user_id=%s", u.id)
            await message.answer("⚠️ Произошла ошибка. Попробуй ещё раз или /clear")
        finally:
            stop_typing.set()
            typing_task.cancel()
