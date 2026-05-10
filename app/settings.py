"""
settings.py — единственный источник правды для всех пользовательских настроек.

Секреты (токены, ключи API) → app/config.py (из .env, read-only)
Настройки пользователя     → app/settings.py (JSON, меняются в runtime через /settings)

Добавить новую настройку = одно поле в UserSettings + одна строка в SETTINGS_SCHEMA.
"""
from __future__ import annotations

import json
import logging
import os
import dataclasses
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_SETTINGS_FILE = Path(os.getenv("SETTINGS_FILE", "data/user_settings.json"))


# ── Схема: что показывать в /settings ────────────────────────────────────────
# Каждый раздел — группа кнопок в Telegram
# type: toggle | int | str | time

SETTINGS_SCHEMA = [
    {
        "section": "🌅 Дайджест",
        "key": "digest_enabled",
        "label": "Утренний дайджест",
        "type": "toggle",
    },
    {
        "section": "🌅 Дайджест",
        "key": "digest_time",
        "label": "Время дайджеста (МСК)",
        "type": "time",
        "hint": "Формат HH:MM, напр. 07:00",
    },
    {
        "section": "🔔 Напоминания",
        "key": "reminder_warning",
        "label": "Предупреждение за 30 мин",
        "type": "toggle",
    },
    {
        "section": "🔔 Напоминания",
        "key": "task_deadlines",
        "label": "Напоминания о дедлайнах",
        "type": "toggle",
    },
    {
        "section": "💬 Проактивность",
        "key": "proactive_silence",
        "label": "Писать первой при тишине",
        "type": "toggle",
    },
    {
        "section": "💬 Проактивность",
        "key": "silence_hours",
        "label": "Часов тишины до инициативы",
        "type": "int",
        "min": 1, "max": 48,
    },
    {
        "section": "💬 Проактивность",
        "key": "proactive_ideas",
        "label": "Follow-up по идеям",
        "type": "toggle",
    },
    {
        "section": "💬 Проактивность",
        "key": "proactive_activity",
        "label": "Предложения активности",
        "type": "toggle",
    },
    {
        "section": "🤖 Модули",
        "key": "module_diary",
        "label": "Дневник",
        "type": "toggle",
    },
    {
        "section": "🤖 Модули",
        "key": "module_calendar",
        "label": "Календарь",
        "type": "toggle",
    },
    {
        "section": "🤖 Модули",
        "key": "module_todo",
        "label": "Задачи",
        "type": "toggle",
    },
    {
        "section": "🤖 Модули",
        "key": "module_deep_research",
        "label": "DEEper (глубокий поиск)",
        "type": "toggle",
    },
    {
        "section": "🤖 Модули",
        "key": "module_ideas",
        "label": "Генератор идей",
        "type": "toggle",
    },
    {
        "section": "🤖 Модули",
        "key": "module_obsidian",
        "label": "Obsidian (заметки, поиск в vault)",
        "type": "toggle",
    },
    {
        "section": "🧠 Модель",
        "key": "temperature",
        "label": "Температура (0.0–2.0)",
        "type": "float",
        "min": 0.0, "max": 2.0,
    },
    {
        "section": "🧠 Модель",
        "key": "max_history",
        "label": "Глубина истории (сообщений)",
        "type": "int",
        "min": 4, "max": 50,
    },
    {
        "section": "🧠 Модель",
        "key": "critic_enabled",
        "label": "Критик (проверка ответов)",
        "type": "toggle",
    },
    {
        "section": "🧠 Модель",
        "key": "memory_enabled",
        "label": "Долгосрочная память",
        "type": "toggle",
    },
    {
        "section": "🎭 Личность",
        "key": "emotional_system",
        "label": "Отслеживание настроения",
        "type": "toggle",
    },
    {
        "section": "🎭 Личность",
        "key": "persona_verbose",
        "label": "Расширенная личность",
        "type": "toggle",
    },
]


@dataclass
class UserSettings:
    # ── Дайджест ──────────────────────────────────────────────────────────────
    digest_enabled: bool  = True
    digest_time:    str   = "06:45"    # HH:MM МСК

    # ── Напоминания ───────────────────────────────────────────────────────────
    reminder_warning: bool = True
    task_deadlines:   bool = True

    # ── Проактивность ─────────────────────────────────────────────────────────
    proactive_silence:   bool = False
    silence_hours:       int  = 4
    proactive_ideas:     bool = True
    proactive_activity:  bool = True

    # ── Модули (on/off) ───────────────────────────────────────────────────────
    module_diary:          bool = True
    module_calendar:       bool = True
    module_todo:           bool = True
    module_deep_research:  bool = True
    module_ideas:          bool = True
    module_obsidian:       bool = True

    # ── Модель ────────────────────────────────────────────────────────────────
    temperature:   float = 0.7
    max_history:   int   = 20
    critic_enabled: bool = True
    memory_enabled: bool = True

    # ── Личность ─────────────────────────────────────────────────────────────
    emotional_system: bool = True
    persona_verbose:  bool = True

    # ── Служебные ────────────────────────────────────────────────────────────
    _extra: dict = field(default_factory=dict, repr=False)

    # ── Свойства-хелперы ─────────────────────────────────────────────────────

    @property
    def digest_hour(self) -> int:
        try:
            return int(self.digest_time.split(":")[0])
        except Exception:
            return 6

    @property
    def digest_minute(self) -> int:
        try:
            return int(self.digest_time.split(":")[1])
        except Exception:
            return 45

    # ── Сериализация ──────────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("_extra", None)
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "UserSettings":
        """Создаёт UserSettings из словаря. Игнорирует неизвестные ключи."""
        # Только поля без _ — публичные настройки
        known = {
            name for name, f in cls.__dataclass_fields__.items()
            if not name.startswith("_")
        }
        kwargs = {}
        for k, v in data.items():
            if k not in known:
                continue
            # Приводим тип к ожидаемому — JSON может хранить int вместо float
            field_default = cls.__dataclass_fields__[k].default
            if field_default is not dataclasses.MISSING:
                try:
                    if isinstance(field_default, bool):
                        v = bool(v)
                    elif isinstance(field_default, float):
                        v = float(v)
                    elif isinstance(field_default, int):
                        v = int(v)
                except (ValueError, TypeError):
                    pass
            kwargs[k] = v
        return cls(**kwargs)

    def set(self, key: str, value: Any) -> bool:
        """Обновить одну настройку. Возвращает True если ключ известен."""
        if not hasattr(self, key) or key.startswith("_"):
            return False
        current = getattr(self, key)
        # Приводим тип
        try:
            if isinstance(current, bool):
                value = bool(value) if not isinstance(value, str) else value.lower() in ("1", "true", "yes", "on")
            elif isinstance(current, int):
                value = int(value)
            elif isinstance(current, float):
                value = float(value)
            else:
                value = str(value)
        except (ValueError, TypeError):
            return False
        setattr(self, key, value)
        return True

    def get(self, key: str, default=None):
        return getattr(self, key, default)


# ── Синглтон ─────────────────────────────────────────────────────────────────

_settings: UserSettings | None = None


def get() -> UserSettings:
    """Вернуть текущие настройки (создать дефолтные если нет)."""
    global _settings
    if _settings is None:
        _settings = _load()
    return _settings


def save() -> None:
    """Сохранить текущие настройки на диск."""
    if _settings is None:
        return
    _save(_settings)


def update(key: str, value: Any) -> bool:
    """Обновить одну настройку и сохранить. Возвращает True если успешно."""
    s = get()
    ok = s.set(key, value)
    if ok:
        _save(s)
        logger.info("⚙️ Настройка обновлена: %s = %r", key, value)
    return ok


def reset() -> None:
    """Сбросить все настройки к дефолтным."""
    global _settings
    _settings = UserSettings()
    _save(_settings)
    logger.info("⚙️ Настройки сброшены к дефолтным")


# ── Внутренние функции ────────────────────────────────────────────────────────

def _load() -> UserSettings:
    _SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    if _SETTINGS_FILE.exists():
        try:
            data = json.loads(_SETTINGS_FILE.read_text(encoding="utf-8"))
            s = UserSettings.from_dict(data)
            logger.info("⚙️ Настройки загружены из %s", _SETTINGS_FILE)
            return s
        except Exception as e:
            logger.warning("⚙️ Не удалось загрузить настройки (%s) — используются дефолтные", e)
    return UserSettings()


def _save(s: UserSettings) -> None:
    try:
        _SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _SETTINGS_FILE.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(s.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(_SETTINGS_FILE)
    except Exception as e:
        logger.error("⚙️ Не удалось сохранить настройки: %s", e)
