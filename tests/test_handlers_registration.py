"""
test_handlers_registration.py — регистрация хендлеров в диспетчере.

Это САМЫЙ важный регрессионный тест в проекте: раньше `handle_photo`,
`handle_document`, `handle_voice` и `handle_message` из-за сломанного отступа
физически оказались вложены внутрь `_transcribe_voice()` ПОСЛЕ return —
мёртвый код, который никогда не регистрировался в диспетчере. Бот отвечал
только на команды (/start и т.п.), а на обычные сообщения, фото, документы
и голосовые — вообще никак. py_compile и импорт модуля такую ошибку не ловят,
только реальная регистрация на настоящем Dispatcher.
"""
from aiogram import Dispatcher


def _handler_names(dp: Dispatcher) -> set[str]:
    return {o.callback.__name__ for o in dp.message.handlers}


def test_all_command_handlers_are_registered(registered_dispatcher):
    expected_commands = {
        "cmd_start", "cmd_help", "cmd_memory", "cmd_forget", "cmd_clear",
        "cmd_reminders", "cmd_stats", "cmd_schedule", "cmd_deeper",
    }
    assert expected_commands.issubset(_handler_names(registered_dispatcher.dp))


def test_all_media_handlers_are_registered(registered_dispatcher):
    """Регрессия прямого попадания: именно эти 4 хендлера были мёртвым кодом."""
    expected_media = {"handle_photo", "handle_document", "handle_voice", "handle_message"}
    assert expected_media.issubset(_handler_names(registered_dispatcher.dp))


def test_callback_query_handler_is_registered(registered_dispatcher):
    callback_names = {o.callback.__name__ for o in registered_dispatcher.dp.callback_query.handlers}
    assert "handle_deeper_mode" in callback_names


def test_no_leftover_vault_command(registered_dispatcher):
    """Regressия: /vault (Obsidian) должен быть полностью убран."""
    assert "cmd_vault" not in _handler_names(registered_dispatcher.dp)


def test_catchall_text_handler_is_last(registered_dispatcher):
    """
    handle_message должен идти ПОСЛЕ всех Command()-хендлеров, иначе он
    перехватит команды раньше своих специализированных обработчиков
    (aiogram проверяет фильтры в порядке регистрации).
    """
    names = [o.callback.__name__ for o in registered_dispatcher.dp.message.handlers]
    assert names[-1] == "handle_message"
    assert names.index("cmd_start") < names.index("handle_message")
