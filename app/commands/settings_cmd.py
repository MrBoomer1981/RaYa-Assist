"""
commands/settings_cmd.py — /settings команда с inline-меню.

Добавить раздел = одна запись в SETTINGS_SCHEMA в app/settings.py.
Этот файл трогать не нужно.

Навигация:
  /settings           → список разделов
  раздел              → список настроек
  настройка           → toggle мгновенно / ввод нового значения
"""
from __future__ import annotations

import logging
from aiogram import Router
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message,
)

import app.settings as S
from app.settings import SETTINGS_SCHEMA

logger = logging.getLogger(__name__)
router = Router()

# user_id → key — ожидаем текстовый ввод
_PENDING_INPUT: dict[int, str] = {}


def _sections() -> list[str]:
    seen: list[str] = []
    for item in SETTINGS_SCHEMA:
        if item["section"] not in seen:
            seen.append(item["section"])
    return seen


def _items_in(section: str) -> list[dict]:
    return [i for i in SETTINGS_SCHEMA if i["section"] == section]


def _fmt_value(item: dict, s: S.UserSettings) -> str:
    val = s.get(item["key"])
    t = item["type"]
    if t == "toggle":
        return "✅" if val else "❌"
    if t == "time":
        return f"🕐 {val}"
    return f"{val}"


def _sections_kb() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text=sec, callback_data=f"cfg:sec:{sec}")]
        for sec in _sections()
    ]
    buttons.append([InlineKeyboardButton(text="🔄 Сбросить к дефолтным", callback_data="cfg:reset")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _section_kb(section: str) -> InlineKeyboardMarkup:
    s = S.get()
    buttons = []
    for item in _items_in(section):
        val_str = _fmt_value(item, s)
        buttons.append([InlineKeyboardButton(
            text=f"{item['label']}  {val_str}",
            callback_data=f"cfg:key:{item['key']}",
        )])
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="cfg:back")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


@router.message(Command("settings"))
async def cmd_settings(message: Message) -> None:
    await message.answer(
        "⚙️ *Настройки RaYa*\n\nВыбери раздел:",
        reply_markup=_sections_kb(),
        parse_mode="Markdown",
    )


@router.callback_query(lambda c: c.data and c.data.startswith("cfg:"))
async def handle_cfg_callback(callback: CallbackQuery) -> None:
    data = callback.data or ""
    parts = data.split(":", 2)
    action = parts[1] if len(parts) > 1 else ""

    if action == "back":
        await callback.message.edit_text(
            "⚙️ *Настройки RaYa*\n\nВыбери раздел:",
            reply_markup=_sections_kb(),
            parse_mode="Markdown",
        )

    elif action == "sec":
        section = parts[2] if len(parts) > 2 else ""
        await callback.message.edit_text(
            f"⚙️ *{section}*",
            reply_markup=_section_kb(section),
            parse_mode="Markdown",
        )

    elif action == "key":
        key = parts[2] if len(parts) > 2 else ""
        item = next((i for i in SETTINGS_SCHEMA if i["key"] == key), None)
        if not item:
            await callback.answer("Настройка не найдена")
            return

        s = S.get()
        if item["type"] == "toggle":
            new_val = not s.get(key)
            S.update(key, new_val)
            await callback.message.edit_text(
                f"⚙️ *{item['section']}*",
                reply_markup=_section_kb(item["section"]),
                parse_mode="Markdown",
            )
            await callback.answer("✅ Включено" if new_val else "❌ Выключено")
            return

        # Нужен текстовый ввод
        _PENDING_INPUT[callback.from_user.id] = key
        current = s.get(key)
        hint = item.get("hint", "")
        bound = ""
        if "min" in item and "max" in item:
            bound = f" (от {item['min']} до {item['max']})"
        await callback.message.edit_text(
            f"⚙️ *{item['label']}*\n\n"
            f"Сейчас: `{current}`\n"
            f"Введи новое значение{bound}:" + (f"\n_{hint}_" if hint else ""),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="◀️ Отмена", callback_data=f"cfg:sec:{item['section']}")
            ]]),
        )

    elif action == "reset":
        S.reset()
        await callback.message.edit_text(
            "🔄 Сброшено к дефолтным.\n\n⚙️ *Настройки RaYa*\n\nВыбери раздел:",
            reply_markup=_sections_kb(),
            parse_mode="Markdown",
        )

    await callback.answer()


def handle_pending_input(user_id: int, text: str) -> str | None:
    """
    Вызывается из handlers.py перед LLM.
    Если пользователь вводил значение настройки — обрабатываем здесь, не идём в LLM.
    Возвращает строку-ответ или None если это не ввод настройки.
    """
    key = _PENDING_INPUT.pop(user_id, None)
    if key is None:
        return None
    item = next((i for i in SETTINGS_SCHEMA if i["key"] == key), None)
    if not item:
        return None
    ok = S.update(key, text.strip())
    if ok:
        val = S.get().get(key)
        return f"✅ *{item['label']}* → `{val}`"
    return f"⚠️ Неверное значение для *{item['label']}*. Попробуй ещё раз."
