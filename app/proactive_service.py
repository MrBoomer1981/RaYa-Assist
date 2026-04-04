"""
proactive.py — проактивные сообщения RaYa: дайджест, тишина и умные триггеры.

Всё в одном файле:
  ProactiveService — основной цикл (утренний дайджест, проверка тишины)
  Triggers         — 5 умных триггеров (напоминания, дедлайны, пауза, дневник, активность)
"""

import itertools
import logging
import os
from pathlib import Path
from app.feature_flags import (
    FEATURE_MORNING_DIGEST, FEATURE_PROACTIVE_ACTIVITY,
    FEATURE_PROACTIVE_IDEA_FOLLOWUP, FEATURE_PROACTIVE_SILENCE,
    FEATURE_REMINDER_WARNING, FEATURE_TASK_DEADLINES,
)
from langchain_core.messages import HumanMessage, SystemMessage
import sqlite3
from datetime import datetime, timedelta

from app.config import settings
from app.database import DB_PATH, get_active_tasks, get_top_interactions, load_history, load_memory, save_messages
from app.database import _conn  # для прямых SQL запросов

# ── Временные константы (секунды) ────────────────────────────────────────────
_1_HOUR   = 3_600
_6_HOURS  = 6 * _1_HOUR
_12_HOURS = 12 * _1_HOUR
_24_HOURS = 24 * _1_HOUR
_48_HOURS = 48 * _1_HOUR


logger = logging.getLogger(__name__)

_MOSCOW_UTC_OFFSET = 3


def _now_msk() -> datetime:
    return datetime.utcnow() + timedelta(hours=_MOSCOW_UTC_OFFSET)


# ── Триггер 1: Напоминание за 30 минут ───────────────────────────────────────

async def check_reminder_warning(user_id: int, bot, llm) -> bool:
    """
    Если через 25-35 минут есть напоминание — предупреждает заранее.
    Возвращает True если сообщение отправлено.
    """
    try:
        now = datetime.utcnow()
        window_start = now + timedelta(minutes=25)
        window_end   = now + timedelta(minutes=35)

        with sqlite3.connect(str(DB_PATH)) as con:
            rows = con.execute("""
                SELECT id, text, remind_at FROM reminders
                WHERE user_id = ? AND done = 0
                  AND remind_at BETWEEN ? AND ?
            """, (
                user_id,
                window_start.strftime("%Y-%m-%d %H:%M:%S"),
                window_end.strftime("%Y-%m-%d %H:%M:%S"),
            )).fetchall()

        if not rows:
            return False

        rid, text, remind_at = rows[0]
        msk_time = (datetime.strptime(remind_at, "%Y-%m-%d %H:%M:%S")
                    + timedelta(hours=_MOSCOW_UTC_OFFSET)).strftime("%H:%M")

        msg = await _gen(llm, (
            f"Сократ, через ~30 минут (в {msk_time}) у тебя напоминание: «{text}». "
            f"Напомни об этом коротко и по-человечески — одно предложение."
        ))
        if msg:
            await bot.send_message(chat_id=user_id, text=msg)
            logger.info("⏰ Reminder warning: '%s'", text[:40])
            return True

    except Exception:
        logger.exception("proactive: reminder warning")
    return False


# ── Триггер 2: Дедлайн задачи ────────────────────────────────────────────────

async def check_task_deadlines(user_id: int, bot, llm, sent_today: set) -> bool:
    """
    Напоминает о задачах с дедлайном сегодня/завтра.
    Читает из Obsidian vault. Фоллбек — БД.
    """
    try:
        overdue = []
        try:
            from app.integrations.obsidian import get_overdue_tasks, vault_available
            if vault_available():
                overdue = get_overdue_tasks(days_threshold=1)
        except Exception:
            pass

        # Фоллбек на БД если vault недоступен
        if not overdue:
            today    = _now_msk().date()
            tomorrow = today + timedelta(days=1)
            with sqlite3.connect(str(DB_PATH)) as con:
                rows = con.execute("""
                    SELECT id, text, due_date FROM tasks
                    WHERE user_id = ? AND done = 0
                      AND due_date IN (?, ?)
                    ORDER BY due_date ASC LIMIT 3
                """, (user_id, str(today), str(tomorrow))).fetchall()
            overdue = [{"text": r[1], "overdue": str(r[2]) < str(today),
                        "deadline": r[2], "hash": r[0]} for r in rows]

        if not overdue:
            return False

        # Фильтруем уже отправленные (по тексту задачи)
        new_tasks = [t for t in overdue if hash(t["text"]) not in sent_today]
        if not new_tasks:
            return False

        task = new_tasks[0]
        when = "сегодня" if not task.get("overdue") else "просрочена"
        msg  = await _gen(llm, (
            f"У Сократа задача {when}: «{task['text']}». "
            f"Напомни коротко — одно предложение, без занудства."
        ))
        if msg:
            await bot.send_message(chat_id=user_id, text=msg)
            sent_today.add(hash(task["text"]))
            logger.info("📋 Task deadline: '%s' (%s)", task["text"][:40], when)
            return True

    except Exception:
        logger.exception("proactive: task deadline")
    return False


async def check_long_absence(user_id: int, bot, llm, last_absence_msg: datetime | None) -> tuple[bool, datetime | None]:
    """
    Если Сократ не писал 2+ дня — RaYa пишет особое сообщение.
    Не чаще раза в 2 дня.
    Возвращает (отправлено, новое_время_последнего_сообщения).
    """
    try:
        last_msg = get_last_message_time(user_id)
        if last_msg is None:
            return False, last_absence_msg

        now = datetime.utcnow()
        silence_hours = (now - last_msg).total_seconds() / _1_HOUR

        if silence_hours < 48:
            return False, last_absence_msg

        # Не отправляли в последние 48ч
        if last_absence_msg and (now - last_absence_msg).total_seconds() < _48_HOURS:
            return False, last_absence_msg

        msg = await _gen(llm, (
            "Сократ не писал уже больше двух дней. Напиши ему короткое живое сообщение — "
            "не 'как дела?', а что-то более конкретное и тёплое. "
            "Можешь упомянуть что заметила его отсутствие. Одно-два предложения."
        ))
        if msg:
            await bot.send_message(chat_id=user_id, text=msg)
            logger.info("👻 Long absence: %.0f ч", silence_hours)
            return True, now

    except Exception:
        logger.exception("proactive: long absence")
    return False, last_absence_msg


# ── Триггер 4: Follow-up по идее из дневника ─────────────────────────────────

async def check_idea_followup(user_id: int, bot, llm, sent_ids: set) -> bool:
    """
    Раз в несколько дней — возвращается к незавершённой идее из дневника.
    Только если есть записи старше 3 дней.
    """
    try:

        cutoff = (datetime.utcnow() - timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")

        with sqlite3.connect(str(DB_PATH)) as con:
            rows = con.execute("""
                SELECT id, entry FROM diary
                WHERE user_id = ? AND created_at < ?
                ORDER BY RANDOM()
                LIMIT 1
            """, (user_id, cutoff)).fetchall()

        if not rows:
            return False

        did, entry = rows[0]
        if did in sent_ids:
            return False

        msg = await _gen(llm, (
            f"Несколько дней назад Сократ записал в дневник: «{entry[:200]}». "
            f"Придумай короткий follow-up — вопрос или наблюдение по этой теме. "
            f"Одно предложение, живо и без занудства."
        ))
        if msg:
            await bot.send_message(chat_id=user_id, text=msg)
            sent_ids.add(did)
            logger.info("💡 Idea follow-up: diary entry %d", did)
            return True

    except Exception:
        logger.exception("proactive: idea followup")
    return False


# ── Триггер 5: Предложение на основе паттернов активности ────────────────────

async def check_activity_suggestion(
    user_id: int, bot, llm,
    last_suggestion: datetime | None
) -> tuple[bool, datetime | None]:
    """
    Анализирует паттерны активности и предлагает что-то полезное.
    Например: обычно активен в это время, но сегодня молчит.
    Не чаще раза в 24ч.
    """
    try:
        now = datetime.utcnow()
        if last_suggestion and (now - last_suggestion).total_seconds() < _24_HOURS:
            return False, last_suggestion
        interactions = get_top_interactions(user_id, limit=3)
        if not interactions:
            return False, last_suggestion

        # Берём самую частую тему
        top_topic, top_summary, freq = interactions[0]
        if freq < 3:
            return False, last_suggestion

        msk_hour = _now_msk().hour
        # Предложения только в рабочие часы
        if not (10 <= msk_hour <= 20):
            return False, last_suggestion

        msg = await _gen(llm, (
            f"Сократ часто возвращается к теме «{top_topic}» ({top_summary}). "
            f"Предложи ему что-то конкретное и полезное по этой теме — "
            f"статью, идею, следующий шаг. Одно предложение, без предисловий."
        ))
        if msg:
            await bot.send_message(chat_id=user_id, text=msg)
            logger.info("💬 Activity suggestion: '%s'", top_topic)
            return True, now

    except Exception:
        logger.exception("proactive: activity suggestion")
    return False, last_suggestion


# ── Генератор сообщений ───────────────────────────────────────────────────────

async def _gen(llm, prompt: str) -> str | None:
    """Генерирует короткое проактивное сообщение."""
    try:
        system = settings.system_prompt + (
            "\n\nКРИТИЧНО: Это проактивное сообщение — ты пишешь первой. "
            "Максимум 1-2 предложения. Никаких списков. Живо и по-человечески. "
            "Обращайся только 'Сократ'."
        )
        response = await llm.ainvoke([
            SystemMessage(content=system),
            HumanMessage(content=prompt),
        ])
        text = str(response.content).strip()
        # Убираем emotion tag если попал
        import re
        text = re.sub(r'<emotion>\w+</emotion>', '', text).strip()
        return text or None
    except Exception:
        logger.exception("proactive: _gen error")
        return None


# ── Главная точка входа ───────────────────────────────────────────────────────

async def check_all_triggers(
    user_id: int,
    bot,
    llm,
    state: dict,
) -> bool:
    """
    Проверяет все триггеры по очереди.
    state — dict с персистентным состоянием между тиками (хранится в ProactiveService).
    Возвращает True если хоть одно сообщение отправлено.
    """
    now_msk = _now_msk()

    # Не проверяем ночью
    if not (8 <= now_msk.hour < 23):
        return False

    sent = False

    # 1. Reminder warning
    if not sent and FEATURE_REMINDER_WARNING:
        sent = await check_reminder_warning(user_id, bot, llm)

    # 2. Task deadlines
    last_task_check = state.get("last_task_check")
    if not sent and FEATURE_TASK_DEADLINES and (not last_task_check or
            (datetime.utcnow() - last_task_check).total_seconds() > _1_HOUR):
        sent_task_ids = state.setdefault("sent_task_ids", set())
        if await check_task_deadlines(user_id, bot, llm, sent_task_ids):
            sent = True
        state["last_task_check"] = datetime.utcnow()

    # 3. Long absence — каждые 6ч
    last_abs_check = state.get("last_absence_check")
    if not sent and (not last_abs_check or
            (datetime.utcnow() - last_abs_check).total_seconds() > _6_HOURS):
        ok, new_ts = await check_long_absence(
            user_id, bot, llm, state.get("last_absence_msg")
        )
        state["last_absence_check"] = datetime.utcnow()
        if ok:
            state["last_absence_msg"] = new_ts
            sent = True

    # 4. Idea follow-up
    last_idea_check = state.get("last_idea_check")
    if not sent and FEATURE_PROACTIVE_IDEA_FOLLOWUP and (not last_idea_check or
            (datetime.utcnow() - last_idea_check).total_seconds() > _12_HOURS):
        sent_diary_ids = state.setdefault("sent_diary_ids", set())
        if await check_idea_followup(user_id, bot, llm, sent_diary_ids):
            sent = True
        state["last_idea_check"] = datetime.utcnow()

    # 5. Activity suggestion
    if not sent and FEATURE_PROACTIVE_ACTIVITY:
        ok, new_ts = await check_activity_suggestion(
            user_id, bot, llm, state.get("last_suggestion")
        )
        if ok:
            state["last_suggestion"] = new_ts
            sent = True

    return sent


# ────────────────────────────────────────────────────────────
# ProactiveService
# ────────────────────────────────────────────────────────────

import asyncio

from aiogram import Bot


logger = logging.getLogger(__name__)

_MOSCOW_UTC_OFFSET  = 3
_DIGEST_HOUR_MSK    = 6      # 06:45 МСК
_DIGEST_MINUTE_MSK  = 45
_SILENCE_HOURS      = 4      # через сколько часов писать первой
_CHECK_INTERVAL_SEC = 60     # проверка каждую минуту


class ProactiveService:

    _STATE_FILE = Path(os.getenv("DB_PATH", "/data/database.db")).parent / "proactive_state.json"

    def __init__(self, bot: Bot, llm_service) -> None:
        self._bot  = bot
        self._llm  = llm_service
        self._task: asyncio.Task | None = None
        self._sched_task: asyncio.Task | None = None

        self._digest_sent_date: str  = ""
        self._last_initiative:  datetime | None = None
        self._trigger_state:    dict = self._load_state()

    _DATETIME_KEYS = (
        "last_task_check", "last_absence_check", "last_idea_check",
        "last_absence_msg", "last_suggestion",
    )

    def _load_state(self) -> dict:
        """Загружает trigger_state с диска — переживает рестарт Railway."""
        try:
            import json as _j
            if self._STATE_FILE.exists():
                data = _j.loads(self._STATE_FILE.read_text())
                # Конвертируем sets обратно
                for key in ("sent_task_ids", "sent_diary_ids"):
                    if key in data:
                        data[key] = set(data[key])
                # Конвертируем datetime-строки обратно в datetime объекты
                for key in self._DATETIME_KEYS:
                    val = data.get(key)
                    if isinstance(val, str):
                        try:
                            data[key] = datetime.fromisoformat(val)
                        except (ValueError, TypeError):
                            del data[key]
                logger.info("📂 Proactive state загружен (%d ключей)", len(data))
                return data
        except Exception as e:
            logger.warning("proactive: load state failed: %s", e)
        return {}

    def _save_state(self) -> None:
        """Сохраняет trigger_state на диск атомарно."""
        try:
            import json as _j
            data = dict(self._trigger_state)
            for key in ("sent_task_ids", "sent_diary_ids"):
                if key in data:
                    data[key] = list(data[key])
            tmp = self._STATE_FILE.with_suffix(".tmp")
            tmp.write_text(_j.dumps(data, ensure_ascii=False, default=str))
            tmp.replace(self._STATE_FILE)
        except Exception as e:
            logger.warning("proactive: save state failed: %s", e)

    def start(self) -> None:
        self._sched_task = asyncio.create_task(self._run_scheduler())
        self._task = asyncio.create_task(self._run())
        logger.info(
            "🌅 Проактивный сервис запущен | дайджест %02d:%02d МСК | тишина %dч",
            _DIGEST_HOUR_MSK, _DIGEST_MINUTE_MSK, _SILENCE_HOURS,
        )

    def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
        if hasattr(self, '_sched_task') and not self._sched_task.done():
            self._sched_task.cancel()

    async def _run_scheduler(self) -> None:
        """Фоновый планировщик напоминаний."""
        while True:
            try:
                await self._tick_scheduler()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Ошибка планировщика напоминаний")
            await asyncio.sleep(60)

    async def _tick_scheduler(self) -> None:
        """Проверяет и отправляет созревшие напоминания."""
        from app.database import get_due_reminders, mark_reminder_done, reschedule_reminder
        now = datetime.utcnow()
        reminders = get_due_reminders(now)
        _RECUR_LABELS = {
            "daily": "каждый день", "weekly": "каждую неделю",
            "weekday": "по будням",  "monthly": "каждый месяц",
        }
        for rid, user_id, text, recurrence in reminders:
            try:
                suffix = ""
                if recurrence and recurrence in _RECUR_LABELS:
                    suffix = f"\n🔁 {_RECUR_LABELS[recurrence]}"
                    reschedule_reminder(rid)
                else:
                    mark_reminder_done(rid)
                await self._bot.send_message(
                    chat_id=user_id,
                    text=f"⏰ Напоминание: {text}{suffix}",
                )
                logger.info("⏰ Напоминание отправлено: '%s' | user_id=%s", text[:40], user_id)
            except Exception:
                logger.exception("Ошибка отправки напоминания id=%s", rid)

    async def _run(self) -> None:
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Ошибка в proactive_service")
            await asyncio.sleep(_CHECK_INTERVAL_SEC)

    async def _tick(self) -> None:
        now_utc = datetime.utcnow()

        # Всё время считаем в МСК
        msk_total_minutes = now_utc.hour * 60 + now_utc.minute + _MOSCOW_UTC_OFFSET * 60
        msk_hour   = (msk_total_minutes // 60) % 24
        msk_minute = msk_total_minutes % 60

        # today по МСК — иначе в 21:00-23:59 МСК дата UTC уже другая
        msk_date = (now_utc + timedelta(hours=_MOSCOW_UTC_OFFSET)).strftime("%Y-%m-%d")

        # ── Утренний дайджест — строго 6:45 МСК, 1 раз в день ──────────────
        # Окно ±2 минуты — защита от пропущенного тика под нагрузкой
        digest_target = _DIGEST_HOUR_MSK * 60 + _DIGEST_MINUTE_MSK
        current_msk   = msk_hour * 60 + msk_minute
        in_window     = abs(current_msk - digest_target) <= 2

        if in_window and self._digest_sent_date != msk_date:
            self._digest_sent_date = msk_date
            await self._send_morning_digest()
            return  # не проверяем тишину в момент дайджеста

        # Переопределяем now_msk_hour для остальной логики
        now_msk_hour = msk_hour

        # ── Инициативное сообщение при тишине ────────────────────────────────
        # Не пишем ночью (23:00 - 08:00 МСК)
        if not (8 <= now_msk_hour < 23):
            return

        # Не пишем чаще чем раз в _SILENCE_HOURS
        if self._last_initiative:
            since_initiative = (now_utc - self._last_initiative).total_seconds() / _1_HOUR
            if since_initiative < _SILENCE_HOURS:
                return

        await self._check_silence(now_utc)

        # ── Умные триггеры проактивности ─────────────────────────────────────
        try:
            from app.database import get_all_known_users
            known_users = get_all_known_users()
            if not known_users:
                return
            user_id = settings.telegram_user_id if settings.telegram_user_id else known_users[0]
            llm     = self._llm._llm
            sent = await check_all_triggers(user_id, self._bot, llm, self._trigger_state)
            if sent:
                self._save_state()
        except Exception:
            logger.exception("Ошибка проактивных триггеров")

    async def _check_silence(self, now_utc: datetime) -> None:
        """Проверяет тишину и пишет первой если надо."""
        if not FEATURE_PROACTIVE_SILENCE:
            return
        try:
            from app.database import get_all_known_users
            known_users = get_all_known_users()
            if not known_users:
                return
            user_id   = settings.telegram_user_id if settings.telegram_user_id else known_users[0]
            last_msg  = get_last_message_time(user_id)

            if last_msg is None:
                return  # нет сообщений вообще — не пишем

            silence_hours = (now_utc - last_msg).total_seconds() / _1_HOUR

            if silence_hours >= _SILENCE_HOURS:
                logger.info("🤫 Тишина %.1f ч — RaYa пишет первой", silence_hours)

                # Берём лёгкую модель для инициативы
                llm = self._llm._llm

                text = await generate_initiative_message(user_id, llm)

                if text:
                    await self._bot.send_message(
                        chat_id=user_id,
                        text=text,
                    )
                    self._last_initiative = now_utc
                    logger.info("✅ Инициативное сообщение отправлено")

        except Exception:
            logger.exception("Ошибка проверки тишины")

    async def _send_morning_digest(self) -> None:
        """Генерирует и отправляет утренний дайджест."""
        try:
            logger.info("🌅 Генерируем утренний дайджест...")
            from app.database import get_all_known_users
            known_users = get_all_known_users()
            if not known_users:
                logger.info("Нет пользователей — дайджест пропущен")
                return
            user_id = settings.telegram_user_id if settings.telegram_user_id else known_users[0]

            from app.agents.morning_agent import MorningAgent
            from app.agents.base_agent import AgentContext

            agent = MorningAgent()
            ctx   = AgentContext(
                user_id=user_id,
                message="утренний дайджест",
                history=load_history(user_id, limit=5),
                memory_facts=load_memory(user_id),
                search_results="",
            )

            result = await agent.run(ctx)

            if result.success and result.content:
                await self._bot.send_message(
                    chat_id=user_id,
                    text=f"🌅 *Доброе утро, Сократ*\n\n{result.content}",
                    parse_mode="Markdown",
                )
                save_messages(user_id, "[утренний дайджест]", result.content)
                logger.info("✅ Утренний дайджест отправлен")

        except Exception:
            logger.exception("Ошибка отправки дайджеста")


# ══════════════════════════════════════════════════════════
# FROM EMOTIONAL SERVICE
# ══════════════════════════════════════════════════════════


# NOTE: unused — candidate for removal
def get_last_message_time(user_id: int) -> datetime | None:
    """Возвращает время последнего сообщения пользователя."""
    try:
        with _conn() as con:
            row = con.execute("""
                SELECT created_at FROM history
                WHERE user_id = ? AND role = 'human'
                ORDER BY created_at DESC LIMIT 1
            """, (user_id,)).fetchone()
        if row:
            return datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S")
        return None
    except Exception:
        logger.exception("get_last_message_time: ошибка")
        return None


_INITIATIVE_PROMPTS = [
    "Сократ давно не писал. Напиши ему короткое живое сообщение — спроси как дела, "
    "поделись чем-то интересным из мира технологий или просто дай знать что ты здесь. "
    "Максимум 2-3 предложения. Без формальностей.",

    "Сократ долго молчит. Напиши ему что-нибудь — короткую мысль, интересный факт "
    "или просто напомни что ты рядом. Живо и по-человечески, 1-2 предложения.",

    "Сократ давно не выходил на связь. Напиши тёплое короткое сообщение. "
    "Можешь упомянуть что-то из его последних разговоров или задач. 1-2 предложения.",
]

_initiative_cycle = itertools.cycle(_INITIATIVE_PROMPTS)

async def generate_initiative_message(user_id: int, llm) -> str:
    """Генерирует инициативное сообщение от RaYa."""
    try:
        facts  = load_memory(user_id)

        context = ""
        if facts:
            context += f"Что RaYa знает о Сократе: {'; '.join(facts[:3])}\n"
        # Читаем задачи из Obsidian если доступен
        try:
            from app.integrations.obsidian import get_all_tasks, vault_available
            if vault_available():
                all_tasks = get_all_tasks()
                active = []
                for q_data in all_tasks.values():
                    active.extend([t["text"] for t in q_data["tasks"] if not t["done"]])
                if active:
                    context += f"Его активные задачи: {', '.join(active[:3])}\n"
            else:
                tasks = get_active_tasks(user_id)
                if tasks:
                    context += f"Его активные задачи: {', '.join(t[1] for t in tasks[:3])}\n"
        except Exception:
            pass

        prompt = next(_initiative_cycle)

        response = await llm.ainvoke([
            SystemMessage(content="Ты RaYa — личный ассистент и друг Сократа. Обращайся только 'Сократ'."),
            HumanMessage(content=prompt + "\n\n" + context),
        ])
        return str(response.content).strip()

    except Exception:
        logger.exception("generate_initiative_message: ошибка")
        return "Сократ, давно не слышала тебя. Всё хорошо?"


# ══════════════════════════════════════════════════════════
# SCHEDULER SERVICE
# ══════════════════════════════════════════════════════════

from datetime import datetime


from app.database import get_due_reminders, mark_reminder_done, reschedule_reminder

logger = logging.getLogger(__name__)

_CHECK_INTERVAL = 60

from app.utils import RECUR_RU as _RECURRENCE_RU
