"""
feature_flags.py — управление функциями RaYa.

Позволяет включать/выключать функции без удаления кода.
Устанавливается через Railway Variables или .env

Переменные (все по умолчанию = "1" если не указано):
  FEATURE_IMAGE_AGENT=0      — генерация изображений
  FEATURE_IDEAS_AGENT=0      — генератор идей
  FEATURE_PROACTIVE_IDEA=0   — idea follow-up триггер
  FEATURE_PROACTIVE_ACTIVITY=0 — activity suggestion триггер
  FEATURE_PERSONA_VERBOSE=1  — расширенные поведенческие паттерны
  FEATURE_EMOTIONAL_SYSTEM=1 — emotional state, mood tracking
"""
import os


def _flag(name: str, default: bool = True) -> bool:
    val = os.getenv(name, "1" if default else "0").strip().lower()
    return val in ("1", "true", "yes", "on")


# ── Агенты ────────────────────────────────────────────────────────────────────
FEATURE_IMAGE_AGENT   = _flag("FEATURE_IMAGE_AGENT",   default=True)
FEATURE_IDEAS_AGENT   = _flag("FEATURE_IDEAS_AGENT",   default=True)

# ── Проактивные триггеры ──────────────────────────────────────────────────────
FEATURE_PROACTIVE_IDEA_FOLLOWUP   = _flag("FEATURE_PROACTIVE_IDEA",     default=True)
FEATURE_PROACTIVE_ACTIVITY        = _flag("FEATURE_PROACTIVE_ACTIVITY", default=True)
FEATURE_PROACTIVE_SILENCE         = _flag("FEATURE_PROACTIVE_SILENCE",  default=False)
FEATURE_MORNING_DIGEST            = _flag("FEATURE_MORNING_DIGEST",     default=True)
FEATURE_TASK_DEADLINES            = _flag("FEATURE_TASK_DEADLINES",     default=True)
FEATURE_REMINDER_WARNING          = _flag("FEATURE_REMINDER_WARNING",   default=True)

# ── Личность ──────────────────────────────────────────────────────────────────
FEATURE_PERSONA_VERBOSE   = _flag("FEATURE_PERSONA_VERBOSE",   default=True)
FEATURE_EMOTIONAL_SYSTEM  = _flag("FEATURE_EMOTIONAL_SYSTEM",  default=True)


def status() -> dict:
    """Возвращает текущий статус всех флагов."""
    return {
        "image_agent":          FEATURE_IMAGE_AGENT,
        "ideas_agent":          FEATURE_IDEAS_AGENT,
        "proactive_idea":       FEATURE_PROACTIVE_IDEA_FOLLOWUP,
        "proactive_activity":   FEATURE_PROACTIVE_ACTIVITY,
        "proactive_silence":    FEATURE_PROACTIVE_SILENCE,
        "morning_digest":       FEATURE_MORNING_DIGEST,
        "task_deadlines":       FEATURE_TASK_DEADLINES,
        "reminder_warning":     FEATURE_REMINDER_WARNING,
        "persona_verbose":      FEATURE_PERSONA_VERBOSE,
        "emotional_system":     FEATURE_EMOTIONAL_SYSTEM,
    }


def get_user_features(user_id: int) -> dict:
    """
    Возвращает feature flags с учётом персональных настроек пользователя.
    Глобальный флаг может только выключить функцию — включить только если
    и глобально включено, и пользователь не выключил.
    """
    from app.user_settings import get_settings
    s = get_settings(user_id)
    return {
        "image_agent":    FEATURE_IMAGE_AGENT    and s.image_agent,
        "ideas_agent":    FEATURE_IDEAS_AGENT    and s.ideas_agent,
        "morning_digest": FEATURE_MORNING_DIGEST and s.morning_digest,
        "task_deadlines": FEATURE_TASK_DEADLINES and s.task_reminders,
        "reminder_warn":  FEATURE_REMINDER_WARNING and s.reminder_warning,
        "proactive_idea": FEATURE_PROACTIVE_IDEA_FOLLOWUP,
        "proactive_act":  FEATURE_PROACTIVE_ACTIVITY,
        "proactive_sil":  FEATURE_PROACTIVE_SILENCE and s.proactive_silence,
        "persona_verbose":FEATURE_PERSONA_VERBOSE  and s.personality_adapt,
        "emotional":      FEATURE_EMOTIONAL_SYSTEM and s.mood_tracking,
        "critic":         s.critic_enabled,
        "memory":         s.memory_enabled,
        "search":         s.search_enabled,
    }
