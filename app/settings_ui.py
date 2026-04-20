"""
settings_ui.py — Telegram inline-UI для настроек пользователя.

Архитектура:
  /settings → список разделов (секций)
  Кнопка секции → список настроек в секции
  Кнопка настройки → изменить значение (toggle / выбор / +/-)
  Кнопка «Назад» → вернуться на уровень выше
  Кнопка «Сброс» → вернуть дефолты

Callback data format:
  s:main                — главное меню настроек
  s:sec:{section_idx}   — меню секции
  s:set:{key}           — меню конкретной настройки
  s:val:{key}:{value}   — установить значение
  s:inc:{key}:{delta}   — инкремент int_range
  s:rst                 — сброс всех настроек
"""
from __future__ import annotations

import logging
from typing import Any

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.user_settings import (
    SECTIONS, SETTINGS_META, UserSettings,
    get_settings, save_settings, update_setting, reset_settings,
)

logger = logging.getLogger(__name__)


# ── Вспомогательные ───────────────────────────────────────────────────────────

def _meta(key: str) -> dict[str, Any]:
    return next(m for m in SETTINGS_META if m["key"] == key)


def _value_label(meta: dict, value: Any) -> str:
    """Форматирует текущее значение для отображения в кнопке."""
    if meta["type"] == "bool":
        return "✅ Вкл" if value else "❌ Выкл"
    if meta["type"] == "choice":
        for k, label in meta["choices"]:
            if k == value:
                return label
        return str(value)
    if meta["type"] == "int_range":
        display_fn = meta.get("display")
        if display_fn:
            return display_fn(value)
        return str(value)
    return str(value)


def _is_accessible(meta: dict, s: UserSettings) -> bool:
    """Проверяет что prerequisite-настройки включены."""
    requires = meta.get("requires", {})
    for k, v in requires.items():
        if getattr(s, k, None) != v:
            return False
    return True


# ── Клавиатуры ────────────────────────────────────────────────────────────────

def kb_main(user_id: int) -> InlineKeyboardMarkup:
    """Главное меню: список разделов."""
    s = get_settings(user_id)
    buttons = []
    for i, section in enumerate(SECTIONS):
        # Считаем кол-во настроек в разделе
        items = [m for m in SETTINGS_META if m["section"] == section]
        buttons.append([InlineKeyboardButton(
            text=f"{section}  ({len(items)})",
            callback_data=f"s:sec:{i}",
        )])
    buttons.append([
        InlineKeyboardButton(text="🔄 Сбросить всё", callback_data="s:rst"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def kb_section(user_id: int, section_idx: int) -> InlineKeyboardMarkup:
    """Меню раздела: список настроек с текущими значениями."""
    section = SECTIONS[section_idx]
    s = get_settings(user_id)
    items = [m for m in SETTINGS_META if m["section"] == section]

    buttons = []
    for meta in items:
        value = getattr(s, meta["key"])
        accessible = _is_accessible(meta, s)
        label = meta["label"]
        val_str = _value_label(meta, value)

        if not accessible:
            # Затемняем недоступные настройки
            text = f"🔒 {label}"
            cb = "s:noop"
        else:
            text = f"{label}  —  {val_str}"
            cb = f"s:set:{meta['key']}"

        buttons.append([InlineKeyboardButton(text=text, callback_data=cb)])

    buttons.append([InlineKeyboardButton(text="◀ Назад", callback_data="s:main")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def kb_setting(user_id: int, key: str) -> InlineKeyboardMarkup:
    """Меню одной настройки: кнопки изменения."""
    meta = _meta(key)
    s = get_settings(user_id)
    value = getattr(s, key)
    section_idx = SECTIONS.index(meta["section"])

    buttons = []

    if meta["type"] == "bool":
        buttons.append([
            InlineKeyboardButton(
                text="✅ Включить" if not value else "✅ Включено (нажми чтобы выкл)",
                callback_data=f"s:val:{key}:true",
            ),
        ])
        buttons.append([
            InlineKeyboardButton(
                text="❌ Выключить" if value else "❌ Выключено (нажми чтобы вкл)",
                callback_data=f"s:val:{key}:false",
            ),
        ])

    elif meta["type"] == "choice":
        for choice_key, choice_label in meta["choices"]:
            is_active = (choice_key == value)
            prefix = "▶ " if is_active else "   "
            buttons.append([InlineKeyboardButton(
                text=f"{prefix}{choice_label}",
                callback_data=f"s:val:{key}:{choice_key}",
            )])

    elif meta["type"] == "int_range":
        min_v = meta["min"]
        max_v = meta["max"]
        step  = meta.get("step", 1)
        display_fn = meta.get("display", str)

        row = []
        if value > min_v:
            row.append(InlineKeyboardButton(
                text=f"−{step}", callback_data=f"s:inc:{key}:-{step}"
            ))
        row.append(InlineKeyboardButton(
            text=f"  {display_fn(value)}  ", callback_data="s:noop"
        ))
        if value < max_v:
            row.append(InlineKeyboardButton(
                text=f"+{step}", callback_data=f"s:inc:{key}:{step}"
            ))
        buttons.append(row)

    buttons.append([InlineKeyboardButton(
        text="◀ Назад", callback_data=f"s:sec:{section_idx}"
    )])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# ── Тексты ────────────────────────────────────────────────────────────────────

def text_main(user_id: int) -> str:
    s = get_settings(user_id)
    return (
        "⚙️ **Настройки RaYa**\n\n"
        "Выбери раздел для изменения:\n"
        f"Язык: {_value_label(_meta('language'), s.language)}  •  "
        f"Стиль: {_value_label(_meta('response_style'), s.response_style)}"
    )


def text_section(section_idx: int) -> str:
    section = SECTIONS[section_idx]
    return f"⚙️ {section}\n\nВыбери настройку:"


def text_setting(user_id: int, key: str) -> str:
    meta = _meta(key)
    s = get_settings(user_id)
    value = getattr(s, key)
    hint = meta.get("hint", "")
    return (
        f"⚙️ **{meta['label']}**\n\n"
        f"Текущее: {_value_label(meta, value)}"
        + (f"\n\n_{hint}_" if hint else "")
    )


# ── Обработчик колбэков ───────────────────────────────────────────────────────

async def handle_settings_callback(
    callback_query,
    user_id: int,
) -> None:
    """Единый обработчик всех s:* колбэков."""
    data = callback_query.data  # type: str
    parts = data.split(":")

    try:
        match parts[1]:
            case "main":
                await callback_query.message.edit_text(
                    text_main(user_id),
                    reply_markup=kb_main(user_id),
                    parse_mode="Markdown",
                )

            case "sec":
                idx = int(parts[2])
                await callback_query.message.edit_text(
                    text_section(idx),
                    reply_markup=kb_section(user_id, idx),
                )

            case "set":
                key = parts[2]
                await callback_query.message.edit_text(
                    text_setting(user_id, key),
                    reply_markup=kb_setting(user_id, key),
                    parse_mode="Markdown",
                )

            case "val":
                key   = parts[2]
                raw   = parts[3]
                meta  = _meta(key)

                # Конвертируем строку в правильный тип
                if meta["type"] == "bool":
                    value: Any = raw == "true"
                elif meta["type"] == "int_range":
                    value = int(raw)
                else:
                    value = raw

                update_setting(user_id, key, value)
                logger.info("settings: user_id=%s %s=%s", user_id, key, value)

                # Обновляем меню настройки
                await callback_query.message.edit_text(
                    text_setting(user_id, key),
                    reply_markup=kb_setting(user_id, key),
                    parse_mode="Markdown",
                )

            case "inc":
                key   = parts[2]
                delta = int(parts[3])
                meta  = _meta(key)
                s     = get_settings(user_id)
                cur   = getattr(s, key)
                new_v = max(meta["min"], min(meta["max"], cur + delta))
                update_setting(user_id, key, new_v)

                await callback_query.message.edit_text(
                    text_setting(user_id, key),
                    reply_markup=kb_setting(user_id, key),
                    parse_mode="Markdown",
                )

            case "rst":
                reset_settings(user_id)
                await callback_query.message.edit_text(
                    "✅ Все настройки сброшены к значениям по умолчанию.",
                    reply_markup=kb_main(user_id),
                )

            case "noop":
                await callback_query.answer()
                return

            case _:
                await callback_query.answer("Неизвестная команда")
                return

    except Exception as e:
        logger.exception("settings callback error: %s", e)
        await callback_query.answer("Ошибка. Попробуй ещё раз.")
        return

    await callback_query.answer()
