"""
test_deeper_command.py — /deeper: единственная точка входа в DEEper.

Текстовые триггеры ("поиск:", "исследуй:") убраны полностью по требованию —
DEEper теперь вызывается только явной командой.
"""
import time as _time
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.filters import CommandObject

import app.handlers as handlers


@pytest.fixture
def cmd_deeper(registered_dispatcher):
    """Достаём саму функцию-хендлер cmd_deeper из общего зарегистрированного Dispatcher."""
    return next(o.callback for o in registered_dispatcher.dp.message.handlers
                if o.callback.__name__ == "cmd_deeper")


@pytest.fixture(autouse=True)
def _clean_state():
    """На всякий случай не даём состоянию одного теста утечь в другой."""
    handlers._PENDING_RESEARCH.clear()
    handlers._AWAITING_TOPIC.clear()
    yield
    handlers._PENDING_RESEARCH.clear()
    handlers._AWAITING_TOPIC.clear()


def _fake_message(chat_id: int, user_id: int = 1):
    msg = MagicMock()
    msg.from_user.id = user_id
    msg.chat.id = chat_id
    msg.answer = AsyncMock()
    return msg


async def test_deeper_with_topic_shows_mode_selection_immediately(cmd_deeper):
    msg = _fake_message(chat_id=100)
    await cmd_deeper(msg, CommandObject(command="deeper", args="квантовые компьютеры"))

    msg.answer.assert_awaited_once()
    text = msg.answer.call_args.args[0]
    assert "квантовые компьютеры" in text
    assert handlers._PENDING_RESEARCH[100] == "квантовые компьютеры"


async def test_bare_deeper_asks_for_topic(cmd_deeper):
    msg = _fake_message(chat_id=200)
    await cmd_deeper(msg, CommandObject(command="deeper", args=None))

    msg.answer.assert_awaited_once()
    assert "тему" in msg.answer.call_args.args[0].lower()
    assert 200 in handlers._AWAITING_TOPIC


def test_awaiting_topic_is_consumed_by_next_message():
    handlers._AWAITING_TOPIC[300] = _time.monotonic()
    assert handlers._consume_awaiting_topic(300) is True
    assert 300 not in handlers._AWAITING_TOPIC  # снято после потребления


def test_awaiting_topic_expires_after_ttl(monkeypatch):
    handlers._AWAITING_TOPIC[400] = 0.0  # искусственно "старый" timestamp
    monkeypatch.setattr(handlers.time, "monotonic", lambda: handlers._AWAITING_TOPIC_TTL + 1000)
    assert handlers._consume_awaiting_topic(400) is False


def test_chat_without_pending_state_returns_false():
    assert handlers._consume_awaiting_topic(999999) is False


def test_old_text_triggers_are_fully_removed():
    """Регрессия: 'поиск:'/'исследуй:' больше не должны существовать как отдельный механизм."""
    assert not hasattr(handlers, "_is_deep_research")
    assert not hasattr(handlers, "_get_search_topic")
    assert not hasattr(handlers, "_SEARCH_CMD_RE")
