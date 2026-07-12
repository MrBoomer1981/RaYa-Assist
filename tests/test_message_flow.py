"""
test_message_flow.py — сквозной сценарий: сообщение → LLM → ответ пользователю.

Это дополнительная защита от регрессии вида "хендлер зарегистрирован, но
реально ничего не делает" — здесь реально вызывается сама функция-хендлер
с фейковым Message и проверяется что message.answer() был вызван с ответом.
"""
from unittest.mock import AsyncMock, MagicMock

from app.llm_service import ChatResult
from tests.conftest import get_handler


def _fake_text_message(text: str, user_id: int = 1, chat_id: int = 1):
    msg = MagicMock()
    msg.text = text
    msg.from_user.id = user_id
    msg.from_user.first_name = "Тест"
    msg.from_user.last_name = ""
    msg.from_user.username = "test_user"
    msg.chat.id = chat_id
    msg.answer = AsyncMock()
    msg.photo = None
    msg.document = None
    msg.voice = None
    return msg


async def test_plain_message_gets_llm_reply(registered_dispatcher, temp_db):
    handle_message = get_handler(registered_dispatcher.dp, "handle_message")
    registered_dispatcher.llm.get_resume_phrase = AsyncMock(return_value=None)
    registered_dispatcher.llm.chat = AsyncMock(
        return_value=ChatResult(reply="Привет! Чем могу помочь?")
    )

    msg = _fake_text_message("Привет, как дела?")
    await handle_message(msg)

    msg.answer.assert_awaited_once_with("Привет! Чем могу помочь?")


async def test_message_with_reminder_saves_it(registered_dispatcher, temp_db):
    handle_message = get_handler(registered_dispatcher.dp, "handle_message")
    registered_dispatcher.llm.get_resume_phrase = AsyncMock(return_value=None)
    registered_dispatcher.llm.chat = AsyncMock(
        return_value=ChatResult(
            reply="Хорошо, напомню!",
            reminder={"text": "Позвонить маме", "remind_at": "2026-08-01 10:00:00"},
        )
    )

    msg = _fake_text_message("напомни позвонить маме 1 августа в 10 утра")
    await handle_message(msg)

    reminders = temp_db.get_active_reminders(1)
    assert any(r[1] == "Позвонить маме" for r in reminders)


async def test_llm_exception_gives_graceful_error_reply(registered_dispatcher, temp_db):
    handle_message = get_handler(registered_dispatcher.dp, "handle_message")
    registered_dispatcher.llm.get_resume_phrase = AsyncMock(return_value=None)
    registered_dispatcher.llm.chat = AsyncMock(side_effect=RuntimeError("Groq недоступен"))

    msg = _fake_text_message("любое сообщение")
    await handle_message(msg)

    msg.answer.assert_awaited_once()
    reply_text = msg.answer.call_args.args[0]
    assert "ошибка" in reply_text.lower()


async def test_awaiting_deeper_topic_intercepts_plain_message(registered_dispatcher, temp_db):
    """Голый /deeper уже спросил тему — следующее обычное сообщение должно уйти в DEEper, а не в LLM chat."""
    import app.handlers as handlers
    handle_message = get_handler(registered_dispatcher.dp, "handle_message")

    msg = _fake_text_message("квантовая физика", chat_id=555)
    handlers._AWAITING_TOPIC[555] = __import__("time").monotonic()
    registered_dispatcher.llm.chat = AsyncMock(
        side_effect=AssertionError("обычный chat() не должен вызываться — тема должна уйти в DEEper")
    )

    await handle_message(msg)

    assert "квантовая физика" in handlers._PENDING_RESEARCH.get(555, "")
