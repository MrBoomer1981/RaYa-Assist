"""
test_handlers_commands.py — команды /memory, /forget, /clear, /reminders, /stats,
/schedule, /start, и обработка фото — прямым вызовом хендлеров.
"""
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

from tests.conftest import get_handler


def _fake_message(user_id: int = 1, first_name: str = "Тест", username: str = "test_user"):
    msg = MagicMock()
    msg.from_user.id = user_id
    msg.from_user.first_name = first_name
    msg.from_user.last_name = ""
    msg.from_user.username = username
    msg.answer = AsyncMock()
    return msg


async def test_cmd_start_greets_by_nickname(registered_dispatcher, temp_db):
    cmd_start = get_handler(registered_dispatcher.dp, "cmd_start")
    msg = _fake_message(username="anna_k")

    await cmd_start(msg)

    msg.answer.assert_awaited_once()
    assert "anna_k" in msg.answer.call_args.args[0]


async def test_cmd_memory_shows_empty_state(registered_dispatcher, temp_db):
    cmd_memory = get_handler(registered_dispatcher.dp, "cmd_memory")
    msg = _fake_message()

    await cmd_memory(msg)

    msg.answer.assert_awaited_once()
    assert "пуста" in msg.answer.call_args.args[0].lower() or "🧠" in msg.answer.call_args.args[0]


async def test_cmd_memory_shows_saved_facts(registered_dispatcher, temp_db):
    from app.services.memory.core import upsert_core_fact
    cmd_memory = get_handler(registered_dispatcher.dp, "cmd_memory")
    upsert_core_fact(1, category="работа", key="профессия", value="дизайнер", importance=5.0)

    msg = _fake_message()
    await cmd_memory(msg)

    text = msg.answer.call_args.args[0]
    assert "дизайнер" in text


async def test_cmd_forget_clears_memory(registered_dispatcher, temp_db):
    cmd_forget = get_handler(registered_dispatcher.dp, "cmd_forget")
    temp_db.save_memory(1, ["Факт для удаления"])

    msg = _fake_message()
    await cmd_forget(msg)

    assert temp_db.load_memory(1) == []
    msg.answer.assert_awaited_once()


async def test_cmd_clear_clears_history(registered_dispatcher, temp_db):
    cmd_clear = get_handler(registered_dispatcher.dp, "cmd_clear")
    temp_db.save_messages(1, "Привет", "Привет!")

    msg = _fake_message()
    await cmd_clear(msg)

    assert temp_db.load_history(1) == []


async def test_cmd_reminders_lists_active(registered_dispatcher, temp_db):
    cmd_reminders = get_handler(registered_dispatcher.dp, "cmd_reminders")
    temp_db.save_reminder(1, "Купить подарок", datetime.now() + timedelta(hours=2))

    msg = _fake_message()
    await cmd_reminders(msg)

    assert "Купить подарок" in msg.answer.call_args.args[0]


async def test_cmd_reminders_empty_state(registered_dispatcher, temp_db):
    cmd_reminders = get_handler(registered_dispatcher.dp, "cmd_reminders")
    msg = _fake_message(user_id=777)  # чистый пользователь без напоминаний

    await cmd_reminders(msg)

    assert "нет" in msg.answer.call_args.args[0].lower()


async def test_cmd_stats_does_not_crash_for_new_user(registered_dispatcher, temp_db):
    cmd_stats = get_handler(registered_dispatcher.dp, "cmd_stats")
    msg = _fake_message(user_id=888)

    await cmd_stats(msg)
    msg.answer.assert_awaited_once()


async def test_cmd_schedule_shows_not_saved_state(registered_dispatcher, temp_db):
    cmd_schedule = get_handler(registered_dispatcher.dp, "cmd_schedule")
    msg = _fake_message(user_id=999)  # без сохранённого расписания

    await cmd_schedule(msg)

    assert "не сохранено" in msg.answer.call_args.args[0].lower()


async def test_cmd_schedule_shows_saved_schedule(registered_dispatcher, temp_db):
    """Регрессия: раньше вся команда падала на 'no such table: schedule_photo'."""
    cmd_schedule = get_handler(registered_dispatcher.dp, "cmd_schedule")
    temp_db.save_schedule_photo(1, "Пн: физика 9:00")

    msg = _fake_message()
    await cmd_schedule(msg)

    assert "физика" in msg.answer.call_args.args[0]
