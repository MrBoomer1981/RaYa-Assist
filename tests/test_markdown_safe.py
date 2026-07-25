"""
test_markdown_safe.py — send_markdown_safe / edit_markdown_safe.

Регрессия на реальный прод-баг: deep research исследование успешно
завершалось и сохранялось (12 минут работы, 32 факта), но отправка
финального отчёта падала с TelegramBadRequest("can't parse entities:
Can't find end of the entity starting at byte offset 5249") — LLM-
сгенерированный текст не гарантированно валидный Telegram Markdown.
Пользователь не получал вообще ничего, сбой был виден только как
"Task exception was never retrieved" в серверных логах.

send_markdown_safe/edit_markdown_safe: пробуют Markdown, при ошибке
разбора сущностей — повторяют тем же текстом без форматирования вместо
того, чтобы полностью терять сообщение.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.exceptions import TelegramBadRequest

from app.utils import edit_markdown_safe, send_markdown_safe


def _bad_request(message: str) -> TelegramBadRequest:
    return TelegramBadRequest(method=MagicMock(), message=message)


# ── send_markdown_safe ────────────────────────────────────────────────────────

async def test_send_markdown_safe_happy_path_sends_once_with_markdown():
    bot = MagicMock(send_message=AsyncMock())

    await send_markdown_safe(bot, 123, "**жирный текст**")

    bot.send_message.assert_awaited_once_with(123, "**жирный текст**", parse_mode="Markdown")


async def test_send_markdown_safe_falls_back_to_plain_text_on_parse_error():
    """Ключевой сценарий бага: тот самый текст ошибки из прод-лога."""
    call_log = []

    async def fake_send(chat_id, text, parse_mode=None, **kw):
        call_log.append(parse_mode)
        if parse_mode == "Markdown":
            raise _bad_request(
                "Bad Request: can't parse entities: Can't find end of the "
                "entity starting at byte offset 5249"
            )

    bot = MagicMock(send_message=AsyncMock(side_effect=fake_send))

    await send_markdown_safe(bot, 123, "Отчёт с *незакрытой звёздочкой")

    assert call_log == ["Markdown", None]  # сначала Markdown, потом fallback на обычный текст
    assert bot.send_message.await_count == 2


async def test_send_markdown_safe_delivers_full_text_on_fallback():
    """Текст при фолбэке должен быть ТЕМ ЖЕ самым — не обрезанным и не изменённым."""
    original_text = "Исследование Макса Ферстаппена: *важный* факт с # незакрытым форматированием"

    async def fake_send(chat_id, text, parse_mode=None, **kw):
        if parse_mode == "Markdown":
            raise _bad_request("can't parse entities: test")
        assert text == original_text

    bot = MagicMock(send_message=AsyncMock(side_effect=fake_send))
    await send_markdown_safe(bot, 123, original_text)


async def test_send_markdown_safe_reraises_non_parse_errors():
    """Другие ошибки Telegram (не про парсинг) не должны тихо проглатываться."""
    bot = MagicMock(send_message=AsyncMock(
        side_effect=_bad_request("Bad Request: message is too long")
    ))

    with pytest.raises(TelegramBadRequest, match="too long"):
        await send_markdown_safe(bot, 123, "текст")

    bot.send_message.assert_awaited_once()  # ретрая не было — ошибка не про парсинг


async def test_send_markdown_safe_passes_through_extra_kwargs():
    bot = MagicMock(send_message=AsyncMock())
    await send_markdown_safe(bot, 123, "текст", reply_to_message_id=99)
    bot.send_message.assert_awaited_once_with(123, "текст", parse_mode="Markdown", reply_to_message_id=99)


# ── edit_markdown_safe ─────────────────────────────────────────────────────────

async def test_edit_markdown_safe_happy_path():
    bot = MagicMock(edit_message_text=AsyncMock())
    await edit_markdown_safe(bot, chat_id=1, message_id=42, text="**прогресс**")

    bot.edit_message_text.assert_awaited_once_with(
        text="**прогресс**", chat_id=1, message_id=42, parse_mode="Markdown"
    )


async def test_edit_markdown_safe_falls_back_on_parse_error():
    call_log = []

    async def fake_edit(text, chat_id, message_id, parse_mode=None, **kw):
        call_log.append(parse_mode)
        if parse_mode == "Markdown":
            raise _bad_request("can't parse entities: byte offset 10")

    bot = MagicMock(edit_message_text=AsyncMock(side_effect=fake_edit))
    await edit_markdown_safe(bot, chat_id=1, message_id=42, text="сломанный * текст")

    assert call_log == ["Markdown", None]


async def test_edit_markdown_safe_reraises_non_parse_errors():
    bot = MagicMock(edit_message_text=AsyncMock(
        side_effect=_bad_request("message to edit not found")
    ))

    with pytest.raises(TelegramBadRequest, match="not found"):
        await edit_markdown_safe(bot, chat_id=1, message_id=42, text="текст")
