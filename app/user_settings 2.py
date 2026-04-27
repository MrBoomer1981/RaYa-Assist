"""
user_settings.py — персональные настройки каждого пользователя.

Хранятся в SQLite таблице user_settings.
Читаются при каждом запросе через LRU-кэш (не дёргаем БД на каждое сообщение).
Изменяются пользователем через /settings в Telegram (inline-кнопки).

Схема настроек — все поля с разумными дефолтами:

  ОБЩИЕ
  ├── language          ru / en              Язык ответов
  ├── response_length   short/medium/long    Длина ответов
  ├── response_style    friendly/formal/concise  Тон общения
  └── timezone          UTC offset -12..+14  Часовой пояс

  ПОИСК
  ├── search_enabled    bool  Включить поиск в интернете
  ├── search_depth      basic/advanced        Глубина поиска
  └── search_lang       auto/ru/en            Язык поиска

  ПРОАКТИВНОСТЬ
  ├── morning_digest    bool  Утренний дайджест
  ├── digest_hour       0-23  Час дайджеста (местное время)
  ├── proactive_silence bool  Писать первой при тишине
  ├── silence_hours     2-24  Через сколько часов писать
  ├── task_reminders    bool  Напоминания о дедлайнах
  └── reminder_warning  bool  Предупреждение за 30 мин

  АГЕНТЫ
  ├── image_agent       bool  Генерация изображений
  ├── ideas_agent       bool  Брейнсторм агент
  └── critic_enabled    bool  Проверка ответов критиком

  ПАМЯТЬ И ЛИЧНОСТЬ
  ├── memory_enabled    bool  Запоминать факты о пользователе
  ├── mood_tracking     bool  Отслеживать настроение
  └── personality_adapt bool  Адаптировать стиль под реакции

  УВЕДОМЛЕНИЯ
  ├── voice_response    bool  Отвечать голосом (TTS)
  └── typing_indicator  bool  Показывать "печатает..."
"""
from __future__ import annotations

import functools
import json
import logging
from dataclasses import asdict, dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# ── Схема настроек ────────────────────────────────────────────────────────────

@dataclass
class UserSettings:
    """Полный набор настроек одного пользователя."""

    # Общие
    language:          str  = "ru"       # ru | en
    response_length:   str  = "medium"   # short | medium | long
    response_style:    str  = "friendly" # friendly | formal | concise
    timezone:          int  = 3          # UTC offset, целое -12..+14

    # Поиск
    search_enabled:    bool = True
    search_depth:      str  = "advanced" # basic | advanced
    search_lang:       str  = "auto"     # auto | ru | en

    # Проактивность
    morning_digest:    bool = True
    digest_hour:       int  = 7          # 0-23 по местному времени
    proactive_silence: bool = False
    silence_hours:     int  = 4          # 2-24
    task_reminders:    bool = True
    reminder_warning:  bool = True

    # Агенты
    image_agent:       bool = True
    ideas_agent:       bool = True
    critic_enabled:    bool = True

    # Память и личность
    memory_enabled:    bool = True
    mood_tracking:     bool = True
    personality_adapt: bool = True

    # Уведомления
    voice_response:    bool = False
    typing_indicator:  bool = True

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: str) -> "UserSettings":
        try:
            data = json.loads(raw)
            # Фильтруем неизвестные поля (защита от старых версий)
            known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
            filtered = {k: v for k, v in data.items() if k in known}
            return cls(**filtered)
        except Exception:
            logger.warning("UserSettings.from_json: ошибка парсинга, дефолты")
            return cls()


# ── Метаданные для UI ─────────────────────────────────────────────────────────

SETTINGS_META: list[dict[str, Any]] = [
    # --- ОБЩИЕ ---
    {
        "key": "language", "section": "🌐 Общие",
        "label": "Язык ответов",
        "type": "choice",
        "choices": [("ru", "🇷🇺 Русский"), ("en", "🇬🇧 English")],
    },
    {
        "key": "response_length", "section": "🌐 Общие",
        "label": "Длина ответов",
        "type": "choice",
        "choices": [
            ("short",  "⚡ Короткие"),
            ("medium", "📝 Средние"),
            ("long",   "📖 Подробные"),
        ],
    },
    {
        "key": "response_style", "section": "🌐 Общие",
        "label": "Стиль общения",
        "type": "choice",
        "choices": [
            ("friendly", "😊 Дружеский"),
            ("formal",   "👔 Формальный"),
            ("concise",  "⚡ Лаконичный"),
        ],
    },
    {
        "key": "timezone", "section": "🌐 Общие",
        "label": "Часовой пояс (UTC±)",
        "type": "int_range",
        "min": -12, "max": 14, "step": 1,
        "hint": "Используется для дайджеста и напоминаний",
    },

    # --- ПОИСК ---
    {
        "key": "search_enabled", "section": "🔍 Поиск",
        "label": "Поиск в интернете",
        "type": "bool",
        "hint": "Искать актуальную информацию при ответах",
    },
    {
        "key": "search_depth", "section": "🔍 Поиск",
        "label": "Глубина поиска",
        "type": "choice",
        "choices": [("basic", "⚡ Быстрый"), ("advanced", "🔬 Глубокий")],
        "requires": {"search_enabled": True},
    },
    {
        "key": "search_lang", "section": "🔍 Поиск",
        "label": "Язык поиска",
        "type": "choice",
        "choices": [("auto", "🤖 Авто"), ("ru", "🇷🇺 Русский"), ("en", "🇬🇧 English")],
        "requires": {"search_enabled": True},
    },

    # --- ПРОАКТИВНОСТЬ ---
    {
        "key": "morning_digest", "section": "🌅 Проактивность",
        "label": "Утренний дайджест",
        "type": "bool",
        "hint": "Новости + задачи каждое утро",
    },
    {
        "key": "digest_hour", "section": "🌅 Проактивность",
        "label": "Время дайджеста",
        "type": "int_range",
        "min": 5, "max": 12, "step": 1,
        "display": lambda v: f"{v:02d}:00",
        "requires": {"morning_digest": True},
    },
    {
        "key": "proactive_silence", "section": "🌅 Проактивность",
        "label": "Писать первой при молчании",
        "type": "bool",
        "hint": "RaYa напишет если долго нет сообщений",
    },
    {
        "key": "silence_hours", "section": "🌅 Проактивность",
        "label": "Часов тишины до сообщения",
        "type": "int_range",
        "min": 2, "max": 24, "step": 1,
        "display": lambda v: f"{v} ч",
        "requires": {"proactive_silence": True},
    },
    {
        "key": "task_reminders", "section": "🌅 Проактивность",
        "label": "Напоминания о дедлайнах",
        "type": "bool",
    },
    {
        "key": "reminder_warning", "section": "🌅 Проактивность",
        "label": "Предупреждение за 30 мин",
        "type": "bool",
        "requires": {"task_reminders": True},
    },

    # --- АГЕНТЫ ---
    {
        "key": "image_agent", "section": "🤖 Агенты",
        "label": "Генерация изображений",
        "type": "bool",
        "hint": "FLUX через Hugging Face (медленнее)",
    },
    {
        "key": "ideas_agent", "section": "🤖 Агенты",
        "label": "Брейнсторм агент",
        "type": "bool",
        "hint": "Генерация идей, SCAMPER",
    },
    {
        "key": "critic_enabled", "section": "🤖 Агенты",
        "label": "Проверка качества ответов",
        "type": "bool",
        "hint": "Критик-редактор проверяет ответы (чуть медленнее)",
    },

    # --- ПАМЯТЬ ---
    {
        "key": "memory_enabled", "section": "🧠 Память",
        "label": "Запоминать факты обо мне",
        "type": "bool",
        "hint": "RaYa запоминает важные детали из разговоров",
    },
    {
        "key": "mood_tracking", "section": "🧠 Память",
        "label": "Отслеживать настроение",
        "type": "bool",
        "hint": "Адаптирует тон под твоё состояние",
    },
    {
        "key": "personality_adapt", "section": "🧠 Память",
        "label": "Адаптировать стиль",
        "type": "bool",
        "hint": "Подстраивает длину и формат под твои реакции",
    },

    # --- УВЕДОМЛЕНИЯ ---
    {
        "key": "voice_response", "section": "🔊 Медиа",
        "label": "Голосовые ответы (TTS)",
        "type": "bool",
        "hint": "Озвучивать ответы голосом",
    },
    {
        "key": "typing_indicator", "section": "🔊 Медиа",
        "label": "Индикатор 'печатает...'",
        "type": "bool",
        "hint": "Показывать что бот думает над ответом",
    },
]

SECTIONS = list(dict.fromkeys(m["section"] for m in SETTINGS_META))


# ── DB функции ────────────────────────────────────────────────────────────────

def _ensure_table() -> None:
    """Создаёт таблицу если не существует (вызывается из init_db)."""
    from app.database import _conn
    with _conn() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id    INTEGER PRIMARY KEY,
                settings   TEXT    NOT NULL DEFAULT '{}',
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)


@functools.lru_cache(maxsize=512)
def _cached_settings(user_id: int) -> UserSettings:
    """LRU-кэшированная загрузка настроек."""
    from app.database import _conn
    with _conn() as con:
        row = con.execute(
            "SELECT settings FROM user_settings WHERE user_id = ?", (user_id,)
        ).fetchone()
    if row:
        return UserSettings.from_json(row[0])
    return UserSettings()


def get_settings(user_id: int) -> UserSettings:
    """Возвращает настройки пользователя (из кэша или БД)."""
    return _cached_settings(user_id)


def save_settings(user_id: int, s: UserSettings) -> None:
    """Сохраняет настройки и инвалидирует кэш."""
    from app.database import _conn
    with _conn() as con:
        con.execute("""
            INSERT INTO user_settings (user_id, settings, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(user_id) DO UPDATE SET
                settings   = excluded.settings,
                updated_at = CURRENT_TIMESTAMP
        """, (user_id, s.to_json()))
    # Инвалидируем кэш
    _cached_settings.cache_clear()


def update_setting(user_id: int, key: str, value: Any) -> UserSettings:
    """Обновляет одну настройку, возвращает обновлённый объект."""
    s = get_settings(user_id)
    if not hasattr(s, key):
        raise ValueError(f"Неизвестная настройка: {key}")
    object.__setattr__(s, key, value)
    save_settings(user_id, s)
    return s


def reset_settings(user_id: int) -> UserSettings:
    """Сбрасывает все настройки к дефолтам."""
    s = UserSettings()
    save_settings(user_id, s)
    return s
