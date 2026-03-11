"""
proactive_triggers.py — умные триггеры для проактивных сообщений RaYa.

Каждый триггер — отдельная проверка с условием и генератором сообщения.
ProactiveService вызывает check_all_triggers() раз в тик.

Триггеры:
  1. Reminder warning  — напоминание за 30 мин до события
  2. Task deadline      — дедлайн задачи сегодня/завтра
  3. Streak break       — Сократ не писал 2+ дня (длинная пауза)
  4. Idea follow-up     — вернуться к незавершённой идее из дневника
  5. Weather warning    — предупреждение если погода резко меняется
"""
import logging
from datetime import datetime, timedelta

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
        import sqlite3
        from app.database import DB_PATH

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
    Если у задачи дедлайн сегодня или завтра — напоминает.
    sent_today — множество task_id уже отправленных сегодня (защита от повтора).
    """
    try:
        import sqlite3
        from app.database import DB_PATH

        today    = _now_msk().date()
        tomorrow = today + timedelta(days=1)

        with sqlite3.connect(str(DB_PATH)) as con:
            rows = con.execute("""
                SELECT id, text, due_date FROM tasks
                WHERE user_id = ? AND done = 0
                  AND due_date IN (?, ?)
                ORDER BY due_date ASC
                LIMIT 3
            """, (user_id, str(today), str(tomorrow))).fetchall()

        if not rows:
            return False

        # Фильтруем уже отправленные
        new_rows = [r for r in rows if r[0] not in sent_today]
        if not new_rows:
            return False

        tid, text, due_date = new_rows[0]
        when = "сегодня" if str(due_date) == str(today) else "завтра"

        msg = await _gen(llm, (
            f"У Сократа дедлайн {when}: «{text}». "
            f"Напомни об этом коротко — одно предложение, без занудства."
        ))
        if msg:
            await bot.send_message(chat_id=user_id, text=msg)
            sent_today.add(tid)
            logger.info("📋 Task deadline: '%s' (%s)", text[:40], when)
            return True

    except Exception:
        logger.exception("proactive: task deadline")
    return False


# ── Триггер 3: Длинная пауза (2+ дня) ────────────────────────────────────────

async def check_long_absence(user_id: int, bot, llm, last_absence_msg: datetime | None) -> tuple[bool, datetime | None]:
    """
    Если Сократ не писал 2+ дня — RaYa пишет особое сообщение.
    Не чаще раза в 2 дня.
    Возвращает (отправлено, новое_время_последнего_сообщения).
    """
    try:
        from app.emotional_service import get_last_message_time

        last_msg = get_last_message_time(user_id)
        if last_msg is None:
            return False, last_absence_msg

        now = datetime.utcnow()
        silence_hours = (now - last_msg).total_seconds() / 3600

        if silence_hours < 48:
            return False, last_absence_msg

        # Не отправляли в последние 48ч
        if last_absence_msg and (now - last_absence_msg).total_seconds() < 48 * 3600:
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
        import sqlite3
        from app.database import DB_PATH

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
        if last_suggestion and (now - last_suggestion).total_seconds() < 24 * 3600:
            return False, last_suggestion

        from app.database import get_top_interactions
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
        from langchain_core.messages import SystemMessage, HumanMessage
        from app.config import settings

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

    # 1. Reminder warning — каждый тик
    if not sent:
        sent = await check_reminder_warning(user_id, bot, llm)

    # 2. Task deadlines — раз в час
    last_task_check = state.get("last_task_check")
    if not sent and (not last_task_check or
            (datetime.utcnow() - last_task_check).total_seconds() > 3600):
        sent_task_ids = state.setdefault("sent_task_ids", set())
        if await check_task_deadlines(user_id, bot, llm, sent_task_ids):
            sent = True
        state["last_task_check"] = datetime.utcnow()

    # 3. Long absence — каждые 6ч
    last_abs_check = state.get("last_absence_check")
    if not sent and (not last_abs_check or
            (datetime.utcnow() - last_abs_check).total_seconds() > 6 * 3600):
        ok, new_ts = await check_long_absence(
            user_id, bot, llm, state.get("last_absence_msg")
        )
        state["last_absence_check"] = datetime.utcnow()
        if ok:
            state["last_absence_msg"] = new_ts
            sent = True

    # 4. Idea follow-up — раз в 3 дня (проверяем раз в 12ч)
    last_idea_check = state.get("last_idea_check")
    if not sent and (not last_idea_check or
            (datetime.utcnow() - last_idea_check).total_seconds() > 12 * 3600):
        sent_diary_ids = state.setdefault("sent_diary_ids", set())
        if await check_idea_followup(user_id, bot, llm, sent_diary_ids):
            sent = True
        state["last_idea_check"] = datetime.utcnow()

    # 5. Activity suggestion — раз в 24ч
    if not sent:
        ok, new_ts = await check_activity_suggestion(
            user_id, bot, llm, state.get("last_suggestion")
        )
        if ok:
            state["last_suggestion"] = new_ts
            sent = True

    return sent
