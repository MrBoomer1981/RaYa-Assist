"""
test_proactive_service.py — проактивные инициативные сообщения и триггеры.

test_empty_memory_does_not_crash — регрессия бага: раньше `user_name`
присваивался ТОЛЬКО внутри `if facts:`, но использовался безусловно чуть ниже —
для пользователя без единого сохранённого факта (типичный случай — новый
пользователь) это падало с UnboundLocalError.
"""
from datetime import datetime, timedelta
from app.utils import utcnow, now_msk
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.proactive_service import (
    generate_initiative_message,
    check_reminder_warning,
    check_task_deadlines,
    check_long_absence,
    check_idea_followup,
)


@pytest.fixture
def llm():
    return MagicMock(ainvoke=AsyncMock(return_value=MagicMock(content="Как твои дела?")))


async def test_empty_memory_does_not_crash(temp_db, llm):
    """Новый пользователь: ни фактов, ни задач — раньше падало с UnboundLocalError."""
    temp_db.upsert_user(1, first_name="Аня", username="")
    result = await generate_initiative_message(1, llm)
    assert result == "Как твои дела?"
    llm.ainvoke.assert_awaited_once()


async def test_with_facts_and_tasks_includes_context_in_prompt(temp_db, llm):
    temp_db.upsert_user(1, first_name="Игорь", username="igor_tech")
    temp_db.save_memory(1, ["Работает программистом", "Любит бег по утрам"])
    temp_db.save_task(1, "Сдать проект", priority=1)

    await generate_initiative_message(1, llm)

    sent_messages = llm.ainvoke.call_args.args[0]
    human_content = sent_messages[1].content
    assert "Работает программистом" in human_content
    assert "Сдать проект" in human_content


async def test_uses_telegram_nickname_in_system_prompt(temp_db, llm):
    """Обращение должно идти по нику из Telegram, а не по общему 'друг'."""
    temp_db.upsert_user(1, first_name="Олег", username="oleg_dev")
    await generate_initiative_message(1, llm)

    sent_messages = llm.ainvoke.call_args.args[0]
    system_content = sent_messages[0].content
    assert "oleg_dev" in system_content


async def test_llm_failure_falls_back_to_generic_message(temp_db):
    temp_db.upsert_user(1, first_name="Соня", username="")
    failing_llm = MagicMock(ainvoke=AsyncMock(side_effect=RuntimeError("Groq недоступен")))

    result = await generate_initiative_message(1, failing_llm)
    assert "Соня" in result
    assert "давно не слышала" in result


# ── Регрессия: _check_silence() падал с NameError на _SILENCE_HOURS ──────────

async def test_check_silence_fires_initiative_message(temp_db, monkeypatch):
    """
    Регрессия: раньше здесь стояло имя `_SILENCE_HOURS`, которого нигде не
    существовало (константу давно заменили настраиваемой `_silence_hours()`,
    но этот вызов не обновили) — падало с NameError при каждой проверке.
    Фича по умолчанию выключена (proactive_silence=False), поэтому баг
    оставался незамеченным, пока пользователь явно не включит её в /settings.

    owner_user_id здесь выставлен явно: раньше при незаданном owner_user_id
    получатель молча угадывался как known_users[0] (минимальный user_id из
    истории) — из-за этого в проде реальный дайджест/проактивные сообщения
    однажды ушли постороннему пользователю, который заблокировал бота, а
    владелец не получил ничего. Теперь без owner_user_id проактивные
    сообщения не отправляются вовсе (см. _resolve_owner_id).
    """
    import app.proactive_service as ps
    import app.feature_flags as ff
    from app.config import settings

    temp_db.upsert_user(1, first_name="Настя", username="nastya_k")
    monkeypatch.setattr(settings, "owner_user_id", 1)
    old_time = (datetime.now() - timedelta(hours=50)).strftime("%Y-%m-%d %H:%M:%S")
    with temp_db._conn() as con:
        con.execute(
            "INSERT INTO history (user_id, role, content, created_at) VALUES (?, 'human', ?, ?)",
            (1, "последнее сообщение", old_time),
        )

    monkeypatch.setattr(ff, "proactive_silence", lambda: True)
    monkeypatch.setattr(ps, "_silence_hours", lambda: 4)

    bot = MagicMock(send_message=AsyncMock())
    llm_service = MagicMock()
    llm_service._llm = MagicMock(ainvoke=AsyncMock(return_value=MagicMock(content="Как ты?")))

    service = ps.ProactiveService(bot, llm_service)
    await service._check_silence(utcnow())

    bot.send_message.assert_awaited_once()
    assert bot.send_message.call_args.kwargs["text"] == "Как ты?"


async def test_check_silence_without_owner_configured_skips_sending(temp_db, monkeypatch):
    """
    Новый тест на сам баг: без OWNER_USER_ID сервис раньше писал наугад
    known_users[0]. Теперь при незаданном owner_user_id проактивные
    сообщения не уходят никому — это безопасное поведение по умолчанию.
    """
    import app.proactive_service as ps
    import app.feature_flags as ff
    from app.config import settings

    temp_db.upsert_user(1, first_name="Настя", username="nastya_k")
    monkeypatch.setattr(settings, "owner_user_id", 0)  # не настроено (dev-режим)
    old_time = (datetime.now() - timedelta(hours=50)).strftime("%Y-%m-%d %H:%M:%S")
    with temp_db._conn() as con:
        con.execute(
            "INSERT INTO history (user_id, role, content, created_at) VALUES (?, 'human', ?, ?)",
            (1, "последнее сообщение", old_time),
        )

    monkeypatch.setattr(ff, "proactive_silence", lambda: True)
    monkeypatch.setattr(ps, "_silence_hours", lambda: 4)

    bot = MagicMock(send_message=AsyncMock())
    llm_service = MagicMock()
    llm_service._llm = MagicMock(ainvoke=AsyncMock(return_value=MagicMock(content="Как ты?")))

    service = ps.ProactiveService(bot, llm_service)
    await service._check_silence(utcnow())

    bot.send_message.assert_not_awaited()


async def test_check_silence_disabled_by_default_does_nothing(temp_db, monkeypatch):
    import app.proactive_service as ps
    import app.feature_flags as ff

    monkeypatch.setattr(ff, "proactive_silence", lambda: False)
    bot = MagicMock(send_message=AsyncMock())
    service = ps.ProactiveService(bot, MagicMock())

    await service._check_silence(utcnow())
    bot.send_message.assert_not_awaited()


# ── Триггер: предупреждение о напоминании за 25-35 минут ──────────────────────

async def test_reminder_warning_fires_within_window(temp_db, llm):
    temp_db.upsert_user(1, first_name="Ира", username="")
    remind_at = datetime.now() + timedelta(minutes=30)
    temp_db.save_reminder(1, "Позвонить дантисту", remind_at)
    bot = MagicMock(send_message=AsyncMock())

    fired = await check_reminder_warning(1, bot, llm)
    assert fired is True
    bot.send_message.assert_awaited_once()


async def test_reminder_warning_silent_outside_window(temp_db, llm):
    temp_db.upsert_user(1, first_name="Ира", username="")
    remind_at = datetime.now() + timedelta(hours=5)  # далеко за окном 25-35 мин
    temp_db.save_reminder(1, "Дальнее напоминание", remind_at)
    bot = MagicMock(send_message=AsyncMock())

    fired = await check_reminder_warning(1, bot, llm)
    assert fired is False
    bot.send_message.assert_not_awaited()


# ── Триггер: дедлайн задачи сегодня/завтра ────────────────────────────────────

async def test_task_deadline_fires_for_task_due_today(temp_db, llm):
    temp_db.upsert_user(1, first_name="Максим", username="")
    # check_task_deadlines считает "сегодня" по МСК (_now_msk()) — тест должен
    # согласованно использовать то же самое, иначе в окне UTC 21:00-23:59
    # (когда в Москве уже следующие сутки) due_date="вчерашняя UTC-дата"
    # никогда не совпадёт с тем, что реально ищет функция.
    today = now_msk().strftime("%Y-%m-%d")
    temp_db.save_task(1, "Сдать отчёт", priority=1, due_date=today)
    bot = MagicMock(send_message=AsyncMock())

    fired = await check_task_deadlines(1, bot, llm, sent_today=set())
    assert fired is True
    bot.send_message.assert_awaited_once()


async def test_task_deadline_does_not_repeat_same_day(temp_db, llm):
    temp_db.upsert_user(1, first_name="Максим", username="")
    today = now_msk().strftime("%Y-%m-%d")
    temp_db.save_task(1, "Сдать отчёт", priority=1, due_date=today)
    bot = MagicMock(send_message=AsyncMock())

    sent_today: set = set()
    await check_task_deadlines(1, bot, llm, sent_today)
    bot.send_message.reset_mock()
    fired_again = await check_task_deadlines(1, bot, llm, sent_today)
    assert fired_again is False
    bot.send_message.assert_not_awaited()


# ── Триггер: долгое молчание ──────────────────────────────────────────────────

async def test_long_absence_fires_after_48_hours(temp_db, llm):
    temp_db.upsert_user(1, first_name="Влад", username="")
    old_time = datetime.now() - timedelta(hours=50)
    with temp_db._conn() as con:
        con.execute(
            "INSERT INTO history (user_id, role, content, created_at) VALUES (?, 'human', ?, ?)",
            (1, "старое сообщение", old_time.strftime("%Y-%m-%d %H:%M:%S")),
        )

    bot = MagicMock(send_message=AsyncMock())
    fired, new_last = await check_long_absence(1, bot, llm, last_absence_msg=None)
    assert fired is True
    assert new_last is not None


async def test_long_absence_silent_for_recent_user(temp_db, llm):
    temp_db.upsert_user(1, first_name="Влад", username="")
    temp_db.save_messages(1, "недавнее сообщение", "ответ")

    bot = MagicMock(send_message=AsyncMock())
    fired, _ = await check_long_absence(1, bot, llm, last_absence_msg=None)
    assert fired is False
    bot.send_message.assert_not_awaited()


async def test_long_absence_no_history_returns_false(temp_db, llm):
    bot = MagicMock(send_message=AsyncMock())
    fired, new_last = await check_long_absence(999, bot, llm, last_absence_msg=None)
    assert fired is False
    assert new_last is None


# ── Триггер: follow-up по дневниковой записи ──────────────────────────────────

async def test_idea_followup_fires_for_old_diary_entry(temp_db, llm):
    temp_db.upsert_user(1, first_name="Марина", username="")
    old_time = (datetime.now() - timedelta(days=5)).strftime("%Y-%m-%d %H:%M:%S")
    with temp_db._conn() as con:
        con.execute(
            "INSERT INTO diary (user_id, entry, created_at) VALUES (?, ?, ?)",
            (1, "Думаю о смене профессии", old_time),
        )

    bot = MagicMock(send_message=AsyncMock())
    fired = await check_idea_followup(1, bot, llm, sent_ids=set())
    assert fired is True


async def test_idea_followup_skips_already_sent(temp_db, llm):
    temp_db.upsert_user(1, first_name="Марина", username="")
    old_time = (datetime.now() - timedelta(days=5)).strftime("%Y-%m-%d %H:%M:%S")
    with temp_db._conn() as con:
        cur = con.execute(
            "INSERT INTO diary (user_id, entry, created_at) VALUES (?, ?, ?)",
            (1, "Запись для теста", old_time),
        )
        diary_id = cur.lastrowid

    bot = MagicMock(send_message=AsyncMock())
    fired = await check_idea_followup(1, bot, llm, sent_ids={diary_id})
    assert fired is False
    bot.send_message.assert_not_awaited()


# ── _send_morning_digest — рассылка по подписчикам ────────────────────────────
# Регрессия: раньше дайджест уходил единственному угаданному получателю
# (known_users[0] при незаданном OWNER_USER_ID — см. миграцию 006 и
# _resolve_owner_id). Теперь это рассылка по users.digest_subscribed, и
# ошибка/блокировка у одного подписчика не должна останавливать остальных.

async def test_send_morning_digest_sends_to_all_subscribers(temp_db, monkeypatch):
    import app.proactive_service as ps
    import app.agents.morning_agent as morning_mod
    from app.agents.base_agent import AgentResult

    temp_db.upsert_user(1, first_name="Настя")
    temp_db.upsert_user(2, first_name="Виктор")
    temp_db.set_digest_subscription(1, True)
    temp_db.set_digest_subscription(2, True)

    class FakeMorningAgent:
        async def run(self, ctx):
            return AgentResult(success=True, content=f"дайджест для {ctx.user_id}", agent_name="morning")

    monkeypatch.setattr(morning_mod, "MorningAgent", FakeMorningAgent)

    bot = MagicMock(send_message=AsyncMock())
    service = ps.ProactiveService(bot, MagicMock())
    await service._send_morning_digest()

    assert bot.send_message.await_count == 2
    sent_to = {c.kwargs["chat_id"] for c in bot.send_message.await_args_list}
    assert sent_to == {1, 2}


async def test_send_morning_digest_blocked_subscriber_does_not_stop_others(temp_db, monkeypatch):
    """Один заблокировавший бота подписчик не должен останавливать рассылку остальным."""
    import app.proactive_service as ps
    import app.agents.morning_agent as morning_mod
    from app.agents.base_agent import AgentResult
    from aiogram.exceptions import TelegramForbiddenError

    temp_db.upsert_user(1, first_name="Настя")
    temp_db.upsert_user(2, first_name="Виктор")
    temp_db.set_digest_subscription(1, True)
    temp_db.set_digest_subscription(2, True)

    class FakeMorningAgent:
        async def run(self, ctx):
            return AgentResult(success=True, content="дайджест", agent_name="morning")

    monkeypatch.setattr(morning_mod, "MorningAgent", FakeMorningAgent)

    async def fake_send_message(chat_id, **kwargs):
        if chat_id == 1:
            raise TelegramForbiddenError(method=MagicMock(), message="Forbidden: bot was blocked by the user")
        return MagicMock()

    bot = MagicMock(send_message=AsyncMock(side_effect=fake_send_message))
    service = ps.ProactiveService(bot, MagicMock())
    await service._send_morning_digest()

    sent_to = {c.kwargs["chat_id"] for c in bot.send_message.await_args_list if c.kwargs.get("chat_id") != 1}
    assert 2 in sent_to
    # заблокировавшего автоматически отписываем, чтобы не долбиться каждое утро
    assert temp_db.is_digest_subscribed(1) is False
    assert temp_db.is_digest_subscribed(2) is True


async def test_send_morning_digest_no_subscribers_sends_nothing(temp_db):
    import app.proactive_service as ps

    bot = MagicMock(send_message=AsyncMock())
    service = ps.ProactiveService(bot, MagicMock())
    await service._send_morning_digest()

    bot.send_message.assert_not_awaited()


async def test_send_morning_digest_respects_global_toggle_in_settings(temp_db, monkeypatch):
    """
    digest_enabled из /settings — общий рубильник. Раньше существовал только
    в SETTINGS_SCHEMA и нигде не проверялся: выключить дайджест через /settings
    было невозможно, переключатель был декорацией.
    """
    import app.proactive_service as ps
    import app.feature_flags as ff

    temp_db.upsert_user(1, first_name="Настя")
    temp_db.set_digest_subscription(1, True)
    monkeypatch.setattr(ff, "morning_digest", lambda: False)

    bot = MagicMock(send_message=AsyncMock())
    service = ps.ProactiveService(bot, MagicMock())
    await service._send_morning_digest()

    bot.send_message.assert_not_awaited()
