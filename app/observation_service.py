"""
observation_service.py — живые наблюдения RaYa о Сократе.

Два механизма:
1. PatternObserver  — замечает паттерны поведения (время суток, повторы тем)
2. RepeatDetector   — замечает если вопрос уже задавался

Результат добавляется в промпт raya_agent — RaYa говорит об этом
своими словами, как живой человек, а не как база данных.
"""
import logging
import re
from datetime import datetime

logger = logging.getLogger(__name__)

_MSK_OFFSET = 3


# ── 1. PatternObserver ────────────────────────────────────────────────────────

def get_pattern_observation(user_id: int) -> str:
    """
    Анализирует паттерны: время активности, частые темы.
    Возвращает наблюдение для промпта или пустую строку.
    Срабатывает редко — примерно каждые 12 сообщений.
    """
    try:
        import sqlite3
        from app.database import DB_PATH

        with sqlite3.connect(str(DB_PATH)) as con:
            rows = con.execute("""
                SELECT created_at FROM history
                WHERE user_id = ? AND role = 'human'
                ORDER BY created_at DESC LIMIT 30
            """, (user_id,)).fetchall()

        if len(rows) < 8:
            return ""

        # Считаем часы активности по МСК
        hours = []
        for (ts,) in rows:
            try:
                dt = datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S")
                msk_hour = (dt.hour + _MSK_OFFSET) % 24
                hours.append(msk_hour)
            except Exception:
                continue

        if not hours:
            return ""

        # Находим пик активности
        late_night = sum(1 for h in hours if 22 <= h or h < 3)
        morning    = sum(1 for h in hours if 6 <= h < 10)
        evening    = sum(1 for h in hours if 19 <= h < 22)

        observations = []

        if late_night >= len(hours) * 0.4:
            observations.append(
                "Сократ часто пишет поздно ночью — возможно сейчас тоже не спит."
            )
        elif morning >= len(hours) * 0.4:
            observations.append(
                "Сократ обычно активен по утрам — сейчас продуктивное время для него."
            )
        elif evening >= len(hours) * 0.4:
            observations.append(
                "Сократ чаще всего пишет вечером — это его время для размышлений."
            )

        if not observations:
            return ""

        return (
            "👁 Наблюдение о паттерне:\n"
            + "\n".join(f"  {o}" for o in observations)
            + "\n  Если уместно — упомяни это одной фразой, не делай из этого тему."
        )

    except Exception:
        logger.debug("pattern_observation: ошибка", exc_info=True)
        return ""


# ── 2. RepeatDetector ─────────────────────────────────────────────────────────

# Минимальное сходство чтобы считать вопрос повтором (0.0–1.0)
_SIMILARITY_THRESHOLD = 0.55

# Сколько последних сообщений проверяем на повтор
_HISTORY_DEPTH = 40


def _simple_similarity(a: str, b: str) -> float:
    """Простое сходство по общим словам (без LLM — мгновенно)."""
    stop = {"и", "в", "на", "с", "по", "что", "как", "это", "не", "а", "я",
            "ты", "он", "она", "мне", "тебе", "можешь", "можно", "есть"}
    wa = set(re.findall(r'\w+', a.lower())) - stop
    wb = set(re.findall(r'\w+', b.lower())) - stop
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / max(len(wa), len(wb))


def get_repeat_observation(user_id: int, current_message: str) -> str:
    """
    Проверяет похож ли текущий вопрос на уже задававшиеся.
    Возвращает инструкцию для промпта или пустую строку.
    """
    try:

        with sqlite3.connect(str(DB_PATH)) as con:
            rows = con.execute("""
                SELECT content, created_at FROM history
                WHERE user_id = ? AND role = 'human'
                ORDER BY created_at DESC LIMIT ?
            """, (user_id, _HISTORY_DEPTH)).fetchall()

        if len(rows) < 3:
            return ""

        # Пропускаем самое последнее (это и есть текущее)
        past_messages = rows[1:]

        best_score = 0.0
        best_match = ""
        best_date  = ""

        for content, ts in past_messages:
            score = _simple_similarity(current_message, content)
            if score > best_score:
                best_score = score
                best_match = content
                best_date  = ts

        if best_score < _SIMILARITY_THRESHOLD:
            return ""

        # Форматируем дату
        try:
            dt       = datetime.strptime(best_date[:19], "%Y-%m-%d %H:%M:%S")
            days_ago = (datetime.utcnow() - dt).days
            if days_ago == 0:
                when = "сегодня"
            elif days_ago == 1:
                when = "вчера"
            else:
                when = f"{days_ago} дней назад"
        except Exception:
            when = "раньше"

        return (
            f"🔁 Похожий вопрос уже был ({when}): «{best_match[:80]}»\n"
            f"  Можешь сослаться на прошлый разговор — это покажет что ты помнишь.\n"
            f"  Например: «Ты уже спрашивал об этом {when}...» — и добавь "
            f"что изменилось или продолжи с того места."
        )

    except Exception:
        logger.debug("repeat_observation: ошибка", exc_info=True)
        return ""


# ── Сборщик для промпта ───────────────────────────────────────────────────────

_observation_counter: dict[int, int] = {}


def build_observation_block(user_id: int, message: str) -> str:
    """
    Собирает наблюдения для системного промпта.
    PatternObserver срабатывает редко (каждые 12 сообщений).
    RepeatDetector — каждый раз.
    """
    _observation_counter[user_id] = _observation_counter.get(user_id, 0) + 1
    count = _observation_counter[user_id]

    parts = []

    # Повтор — каждый раз
    repeat = get_repeat_observation(user_id, message)
    if repeat:
        parts.append(repeat)

    # Паттерн — редко
    if count % 12 == 0:
        pattern = get_pattern_observation(user_id)
        if pattern:
            parts.append(pattern)

    return "\n\n".join(parts)
