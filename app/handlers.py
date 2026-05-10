"""
handlers.py — Telegram хендлеры (single-user версия).

Убрано: voice/TTS, image agent, settings UI, rate limiting (не нужен single-user),
        multi-user семафор.
Оставлено: текст, фото (vision), документы, команды, deep research прогресс.
"""
import asyncio
import logging
import tempfile
from datetime import datetime
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import Message

from app.config import settings
from app.database import (
    clear_history, clear_memory, load_memory,
    save_reminder, get_active_reminders, get_memory_by_category,
    upsert_user, get_user_name,
)
from app.document_service import SUPPORTED_EXTENSIONS, extract_text
from app.llm_service import LLMService, ChatResult
from app.vision_service import VisionService

logger = logging.getLogger(__name__)

_MAX_FILE_BYTES = 20 * 1024 * 1024  # 20 МБ

from app.utils import RECUR_RU


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
    if settings.obsidian_enabled:
        lines.append("• Синхронизироваться с Obsidian 🗂️")
    lines += [
        "\nКоманды:",
        "/reminders — активные напоминания",
        "/memory    — что знаю о тебе",
        "/forget    — удалить память",
        "/stats     — статистика за неделю",
        "/clear     — очистить историю разговора",
        "/settings  — настройки (время дайджеста, модули, модель)",
        "/vault     — статус Obsidian vault",
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


async def _send_deep_research(message: Message, bot: Bot) -> None:
    """
    Запускает DEEper с живым прогрессом через edit_message.
    Пользователь видит каждый шаг исследования в реальном времени.
    """
    from app.agents.deep_research_agent import _get_bridge, _MODE_LABELS

    # ── Отправляем начальное сообщение ────────────────────────────────
    status_msg = await message.answer("🔬 Начинаю глубокое исследование...")
    progress_lines: list[str] = []

    async def _update(line: str) -> None:
        progress_lines.append(line)
        display = "\n".join(progress_lines[-7:])
        try:
            await bot.edit_message_text(
                text=f"🔬 *Исследование...*\n\n{display}",
                chat_id=message.chat.id,
                message_id=status_msg.message_id,
                parse_mode="Markdown",
            )
        except Exception:
            pass  # edit_message может упасть если сообщение удалено

    try:
        bridge = _get_bridge()
    except Exception as e:
        await bot.edit_message_text(
            text=f"⚠️ DEEper не запустился: {e}",
            chat_id=message.chat.id,
            message_id=status_msg.message_id,
        )
        return

    query = message.text or ""
    import time
    start = time.monotonic()

    try:
        result = await bridge.research(
            topic=query,
            mode="deep",
            progress_cb=_update,
        )
    except Exception as e:
        await bot.edit_message_text(
            text=f"⚠️ Ошибка исследования: {e}",
            chat_id=message.chat.id,
            message_id=status_msg.message_id,
        )
        return

    elapsed  = round(time.monotonic() - start, 1)
    report   = result.get("report", "Отчёт не сформирован.")
    sources  = result.get("sources", [])
    res_id   = result.get("id")

    footer = f"\n\n---\n🔬 Углублённый | ⏱ {elapsed}с | 📚 {len(sources)} источников"
    if res_id:
        footer += f" | ID: {res_id}"

    try:
        await bot.delete_message(message.chat.id, status_msg.message_id)
    except Exception:
        pass  # MessageToDeleteNotFound — сообщение уже удалено, OK

    full_report = report + footer
    chunks = _split_report(full_report, max_len=3800)
    for i, chunk in enumerate(chunks):
        prefix = f"📚 *Отчёт (часть {i+1}/{len(chunks)})*\n\n" if len(chunks) > 1 else ""
        await message.answer(prefix + chunk, parse_mode="Markdown")


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
    """Отправляет ответ пользователю. TTS убран — только текст."""
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



_DEEP_RESEARCH_TRIGGERS = frozenset({
    "глубокое исследование", "deep research", "подробный анализ",
    "исследуй глубоко", "детальный отчёт", "полный анализ",
    "всё о", "расскажи подробно всё", "подготовь отчёт",
    "аналитика по", "детально изучи", "развёрнутый анализ",
})

def _is_deep_research(message: str) -> bool:
    msg = message.lower()
    return any(t in msg for t in _DEEP_RESEARCH_TRIGGERS)

# ── Статистика ────────────────────────────────────────────────────────────────

def _build_stats(user_id: int) -> str:
    from datetime import datetime, timedelta
    from app.database import _conn

    now      = datetime.utcnow()
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


    @dp.message(Command("vault"))
    async def cmd_vault(message: Message) -> None:
        """Показывает статус Obsidian и корневые папки."""
        if not message.from_user:
            return
        from app.config import settings as _cfg
        from app.services.obsidian import ping, list_folder
        if not _cfg.obsidian_enabled:
            await message.answer(
                "⚠️ Obsidian не настроен.\n\n"
                "Добавь в Railway Variables:\n"
                "`OBSIDIAN_API_URL=https://127.0.0.1:27124`\n"
                "`OBSIDIAN_API_KEY=твой_ключ`"
            )
            return
        ok = await ping()
        if not ok:
            await message.answer(
                f"❌ Obsidian недоступен.\n"
                f"URL: `{_cfg.obsidian_api_url}`\n"
                "Проверь что плагин запущен и API включён."
            )
            return
        try:
            files = await list_folder("")
            folders = [f for f in files if not "." in f.split("/")[-1]]
            lines = [f"✅ Obsidian подключён\n`{_cfg.obsidian_api_url}`\n"]
            if folders:
                lines.append("📂 Папки в vault:")
                lines.extend(f"  • {f}" for f in folders[:15])
            else:
                lines.append(f"Файлов в vault: {len(files)}")
            await message.answer("\n".join(lines))
        except Exception as e:
            await message.answer(f"✅ Obsidian доступен, но ошибка листинга: {e}")

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
            # Deep research: отдельный flow с live-прогрессом DEEper
            if _is_deep_research(message.text or ""):
                stop_typing.set()
                typing_task.cancel()
                await _send_deep_research(message, bot)
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
