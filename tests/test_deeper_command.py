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


# ── _run_deep_research — таймаут ──────────────────────────────────────────────
# Регрессия: у этого пути (кнопки выбора режима после /deeper) раньше НЕ было
# вообще никакого таймаута на bridge.research() — при зависании (например,
# каскад рейт-лимитов Groq) запрос мог висеть неограниченно долго, и
# пользователь не получал вообще ничего — ни отчёта, ни ошибки.

async def test_run_deep_research_success_sends_report(monkeypatch):
    import app.agents.deep_research_agent as dra_mod

    class FakeBridge:
        async def research(self, topic, mode, progress_cb=None):
            if progress_cb:
                await progress_cb("шаг 1: ищу источники")
            return {"report": "Готовый отчёт по теме.", "sources": ["a.com", "b.com"], "id": 42}

    monkeypatch.setattr(dra_mod, "_get_bridge", lambda: FakeBridge())

    bot = MagicMock()
    bot.edit_message_text = AsyncMock()
    bot.delete_message = AsyncMock()
    bot.send_message = AsyncMock()

    await handlers._run_deep_research(chat_id=1, topic="квантовые компьютеры", mode="simple",
                                       bot=bot, status_msg_id=99)

    bot.send_message.assert_awaited()
    sent_text = bot.send_message.call_args.args[1]
    assert "Готовый отчёт" in sent_text


async def test_run_deep_research_delivers_report_despite_broken_markdown(monkeypatch):
    """
    Регрессия на реальный прод-баг: deep research по теме "Макс Ферстаппен"
    успешно завершился (32 факта, отчёт сохранён), но отправка упала с
    TelegramBadRequest("can't parse entities: Can't find end of the entity
    starting at byte offset 5249") — LLM-отчёт содержал несбалансированную
    Markdown-разметку. Пользователь не получил вообще ничего, а в логах
    осталось только невнятное "Task exception was never retrieved".

    Теперь send_markdown_safe должен доставить тот же текст обычным
    сообщением, если Markdown не распарсился.
    """
    import app.agents.deep_research_agent as dra_mod
    from aiogram.exceptions import TelegramBadRequest

    broken_report = "Ферстаппен *трёхкратный чемпион мира без закрытия звёздочки и весь остальной текст"

    class FakeBridge:
        async def research(self, topic, mode, progress_cb=None):
            return {"report": broken_report, "sources": ["f1.com"], "id": 7}

    monkeypatch.setattr(dra_mod, "_get_bridge", lambda: FakeBridge())

    async def fake_send(chat_id, text, parse_mode=None, **kw):
        if parse_mode == "Markdown":
            raise TelegramBadRequest(
                method=MagicMock(),
                message="Bad Request: can't parse entities: Can't find end of the "
                        "entity starting at byte offset 5249",
            )
        # fallback без parse_mode должен пройти успешно

    bot = MagicMock()
    bot.edit_message_text = AsyncMock()
    bot.delete_message = AsyncMock()
    bot.send_message = AsyncMock(side_effect=fake_send)

    await handlers._run_deep_research(chat_id=1, topic="Макс Ферстаппен", mode="deep",
                                       bot=bot, status_msg_id=99)

    # Отчёт должен уйти пользователю (вторым вызовом, без parse_mode),
    # а не потеряться молча
    assert bot.send_message.await_count == 2
    delivered_calls = [c for c in bot.send_message.await_args_list if c.kwargs.get("parse_mode") is None]
    assert len(delivered_calls) == 1
    assert "Ферстаппен" in delivered_calls[0].args[1]


async def test_run_deep_research_notifies_user_if_delivery_totally_fails(monkeypatch):
    """
    Страховка верхнего уровня: если доставка не удалась вообще никак (не
    только Markdown-парсинг) — пользователь должен получить хоть какое-то
    сообщение, а не полную тишину после успешного (и потенциально долгого)
    исследования.
    """
    import app.agents.deep_research_agent as dra_mod

    class FakeBridge:
        async def research(self, topic, mode, progress_cb=None):
            return {"report": "Отчёт", "sources": [], "id": 99}

    monkeypatch.setattr(dra_mod, "_get_bridge", lambda: FakeBridge())

    bot = MagicMock()
    bot.edit_message_text = AsyncMock()
    bot.delete_message = AsyncMock()
    # Всё падает, включая попытку уведомить об ошибке — не должно кидать наружу
    bot.send_message = AsyncMock(side_effect=RuntimeError("сеть недоступна"))

    await handlers._run_deep_research(chat_id=1, topic="тема", mode="simple",
                                       bot=bot, status_msg_id=99)

    # Убеждаемся, что была хотя бы ПОПЫТКА уведомить пользователя
    assert bot.send_message.await_count >= 1


async def test_run_deep_research_timeout_shows_clear_message_not_hang(monkeypatch):
    """
    Раньше: bridge.research() без таймаута мог зависнуть навсегда.
    Теперь: asyncio.wait_for обрывает по RESEARCH_MODES[mode].timeout_sec
    и показывает понятное сообщение вместо вечного "печатает...".
    """
    import asyncio
    import app.agents.deep_research_agent as dra_mod
    import deeper.config as deeper_config_mod

    # Не ждём реальные минуты в тесте — подменяем потолок на копейки.
    monkeypatch.setattr(deeper_config_mod.RESEARCH_MODES["simple"], "timeout_sec", 0.05)

    class HangingBridge:
        async def research(self, topic, mode, progress_cb=None):
            await asyncio.sleep(2)  # дольше таймаута — эмулирует зависание
            return {"report": "сюда не должны дойти"}

    monkeypatch.setattr(dra_mod, "_get_bridge", lambda: HangingBridge())

    bot = MagicMock()
    bot.edit_message_text = AsyncMock()
    bot.send_message = AsyncMock()

    await handlers._run_deep_research(chat_id=1, topic="тема", mode="simple",
                                       bot=bot, status_msg_id=99)

    # Пользователь должен получить понятное сообщение, а не зависание навсегда
    bot.edit_message_text.assert_awaited()
    last_text = bot.edit_message_text.call_args.kwargs.get("text", "")
    assert "⚠️" in last_text and "мин" in last_text
    bot.send_message.assert_not_awaited()  # отчёта нет — таймаут сработал раньше


async def test_run_deep_research_uses_per_mode_timeout(monkeypatch):
    """Разные режимы должны использовать РАЗНЫЙ потолок из RESEARCH_MODES, а не один общий."""
    import asyncio
    import app.agents.deep_research_agent as dra_mod
    import deeper.config as deeper_config_mod

    seen_timeouts = []
    real_wait_for = asyncio.wait_for

    async def spying_wait_for(coro, timeout):
        seen_timeouts.append(timeout)
        return await real_wait_for(coro, timeout=timeout)

    monkeypatch.setattr(handlers.asyncio, "wait_for", spying_wait_for)

    class FakeBridge:
        async def research(self, topic, mode, progress_cb=None):
            return {"report": "ок", "sources": [], "id": 1}

    monkeypatch.setattr(dra_mod, "_get_bridge", lambda: FakeBridge())

    bot = MagicMock()
    bot.edit_message_text = AsyncMock()
    bot.delete_message = AsyncMock()
    bot.send_message = AsyncMock()

    await handlers._run_deep_research(chat_id=1, topic="тема", mode="study", bot=bot, status_msg_id=1)

    assert seen_timeouts == [deeper_config_mod.RESEARCH_MODES["study"].timeout_sec]
    assert seen_timeouts[0] == 1200
