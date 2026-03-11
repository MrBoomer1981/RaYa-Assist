"""
personality_service.py — единый сервис осознанности личности RaYa.

Объединяет 4 механизма:
  1. Mood Mirroring       — зеркалит энергетику сообщения
  2. Personality Feedback — адаптирует стиль по реакции Сократа
  3. Thematic Depth       — углубляется в повторяющиеся темы
  4. Emotional Memory     — паттерны настроения по времени/дням

Все механизмы работают фоново, не блокируя ответ.
Результат добавляется в системный промпт raya_agent.
"""
import logging
import re
from collections import Counter
from datetime import datetime

logger = logging.getLogger(__name__)


# ── 1. Mood Mirroring ─────────────────────────────────────────────────────────

def get_mirror_hint(message: str) -> str:
    """
    Анализирует энергетику сообщения и возвращает инструкцию для промпта.
    Быстро, без LLM — по эвристикам.
    """
    text  = message.strip()
    words = text.split()
    chars = len(text)

    # Энергетика по длине и пунктуации
    if chars < 25 or len(words) <= 4:
        return "Сократ пишет кратко — отвечай так же лаконично, не растекайся."

    exclamations = text.count("!")
    questions    = text.count("?")
    caps_ratio   = sum(1 for c in text if c.isupper()) / max(len(text), 1)

    if exclamations >= 2 or caps_ratio > 0.15:
        return "Сократ пишет энергично — поддержи его тон, будь живой и активной."

    if questions >= 2:
        return "Сократ задаёт много вопросов — будь конкретной, отвечай по существу."

    if chars > 300:
        return "Сократ написал развёрнуто — можешь ответить подробнее, он в разговорном режиме."

    return ""  # нейтральная энергетика — без подсказок


# ── 2. Personality Feedback Loop ─────────────────────────────────────────────

# Категория в structured_memory где хранится feedback
_FEEDBACK_CATEGORY = "style_feedback"

# Минимум сообщений для анализа паттерна
_MIN_MESSAGES_FOR_FEEDBACK = 6


async def update_feedback(user_id: int, llm) -> None:
    """
    Фоново анализирует последние обмены и обновляет стилевые предпочтения.
    Смотрит: что вызывает продолжение диалога, короткие или длинные ответы лучше.
    """
    try:
        from app.database import load_history, upsert_memory
        from langchain_core.messages import HumanMessage

        history = load_history(user_id, limit=10)
        if len(history) < _MIN_MESSAGES_FOR_FEEDBACK:
            return

        # Считаем длины ответов RaYa и длины следующих сообщений Сократа
        pairs = []
        for i in range(len(history) - 1):
            msg  = history[i]
            next_msg = history[i + 1]
            if (msg.__class__.__name__   == "AIMessage" and
                    next_msg.__class__.__name__ == "HumanMessage"):
                pairs.append({
                    "raya_len":   len(msg.content),
                    "sokrat_len": len(next_msg.content),
                })

        if not pairs:
            return

        # Простая эвристика: если Сократ отвечает длинно — ему нравится диалог
        avg_sokrat_after_long  = sum(p["sokrat_len"] for p in pairs if p["raya_len"] > 300) / max(1, sum(1 for p in pairs if p["raya_len"] > 300))
        avg_sokrat_after_short = sum(p["sokrat_len"] for p in pairs if p["raya_len"] <= 300) / max(1, sum(1 for p in pairs if p["raya_len"] <= 300))

        if avg_sokrat_after_short > avg_sokrat_after_long * 1.3:
            style_pref = "короткие ответы — Сократ охотнее продолжает диалог"
        elif avg_sokrat_after_long > avg_sokrat_after_short * 1.3:
            style_pref = "развёрнутые ответы — Сократ вовлекается сильнее"
        else:
            style_pref = "нейтрально — длина ответа не влияет"

        upsert_memory(user_id, _FEEDBACK_CATEGORY, "длина_ответов", style_pref)
        logger.info("📊 Feedback: %s | user_id=%s", style_pref, user_id)

    except Exception:
        logger.exception("personality: ошибка feedback loop")


def get_feedback_hint(user_id: int) -> str:
    """Возвращает инструкцию на основе накопленного feedback."""
    try:
        from app.database import get_memory_by_category
        feedback = get_memory_by_category(user_id, _FEEDBACK_CATEGORY)
        if not feedback:
            return ""

        parts = []
        if "длина_ответов" in feedback:
            pref = feedback["длина_ответов"]
            if "короткие" in pref:
                parts.append("По наблюдениям: Сократ лучше реагирует на короткие ответы.")
            elif "развёрнутые" in pref:
                parts.append("По наблюдениям: Сократ вовлекается сильнее когда отвечаешь подробно.")

        return " ".join(parts)
    except Exception:
        return ""


# ── 3. Thematic Depth ─────────────────────────────────────────────────────────

# Сколько раз тема должна встретиться чтобы считаться "повторяющейся"
_TOPIC_THRESHOLD = 3

# Ключевые тематические слова → название темы
_TOPIC_PATTERNS = {
    "python":     ["python", "питон", "asyncio", "django", "flask", "fastapi"],
    "rust":       ["rust", "cargo", "borrow", "ownership"],
    "telegram":   ["telegram", "бот", "aiogram", "webhook"],
    "railway":    ["railway", "деплой", "deploy", "nixpacks"],
    "база_данных":["sqlite", "postgres", "база данных", "sql", "orm"],
    "ai_ml":      ["нейросеть", "llm", "groq", "модель", "промпт", "embedding"],
    "архитектура":["архитектура", "паттерн", "рефакторинг", "solid", "микросервис"],
    "продуктивность": ["задачи", "планирование", "дедлайн", "фокус", "прокрастинация"],
}


def get_depth_hint(user_id: int) -> str:
    """
    Если тема встречается часто в истории — предлагает углубиться.
    Возвращает инструкцию для промпта или пустую строку.
    """
    try:
        from app.database import load_history

        history = load_history(user_id, limit=20)
        if len(history) < _TOPIC_THRESHOLD:
            return ""

        all_text = " ".join(m.content.lower() for m in history
                            if m.__class__.__name__ == "HumanMessage")

        # Считаем попадания по темам
        topic_counts: Counter = Counter()
        for topic, keywords in _TOPIC_PATTERNS.items():
            hits = sum(all_text.count(kw) for kw in keywords)
            if hits > 0:
                topic_counts[topic] = hits

        if not topic_counts:
            return ""

        top_topic, count = topic_counts.most_common(1)[0]
        if count < _TOPIC_THRESHOLD:
            return ""

        topic_labels = {
            "python":         "Python/async разработку",
            "rust":           "Rust",
            "telegram":       "Telegram ботов",
            "railway":        "Railway деплой",
            "база_данных":    "базы данных",
            "ai_ml":          "LLM и AI разработку",
            "архитектура":    "архитектуру кода",
            "продуктивность": "продуктивность и планирование",
        }

        label = topic_labels.get(top_topic, top_topic)
        return (
            f"Сократ часто возвращается к теме: {label}. "
            f"Если уместно — задай более глубокий вопрос или предложи следующий шаг."
        )
    except Exception:
        return ""


# ── 4. Emotional Memory ───────────────────────────────────────────────────────

_MOOD_CATEGORY = "emotional_patterns"

_DAY_NAMES = {
    0: "понедельник", 1: "вторник", 2: "среда",
    3: "четверг", 4: "пятница", 5: "суббота", 6: "воскресенье",
}

_NEGATIVE_MOODS = {"stressed", "sad", "anxious", "tired", "angry", "frustrated"}
_POSITIVE_MOODS = {"happy", "excited", "focused", "calm", "confident"}


async def update_emotional_patterns(user_id: int) -> None:
    """
    Фоново анализирует паттерны настроения по дням недели.
    Сохраняет в structured_memory[emotional_patterns].
    """
    try:
        from app.database import get_recent_moods, upsert_memory

        # Нужна история с временными метками — берём из mood_log
        import sqlite3
        from app.database import DB_PATH

        with sqlite3.connect(str(DB_PATH)) as con:
            rows = con.execute("""
                SELECT mood, created_at FROM mood_log
                WHERE user_id = ?
                ORDER BY created_at DESC
                LIMIT 30
            """, (user_id,)).fetchall()

        if len(rows) < 5:
            return

        # Группируем по дням недели
        day_moods: dict[int, list[str]] = {i: [] for i in range(7)}
        for mood, ts in rows:
            try:
                dt  = datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S")
                day_moods[dt.weekday()].append(mood.lower())
            except Exception:
                continue

        # Находим дни с преобладающим негативным настроением
        stress_days = []
        for day, moods in day_moods.items():
            if len(moods) < 2:
                continue
            neg_ratio = sum(1 for m in moods if m in _NEGATIVE_MOODS) / len(moods)
            if neg_ratio >= 0.5:
                stress_days.append(_DAY_NAMES[day])

        if stress_days:
            pattern = "часто в стрессе по: " + ", ".join(stress_days)
            upsert_memory(user_id, _MOOD_CATEGORY, "стресс_паттерн", pattern)
            logger.info("🧠 Emotional pattern: %s | user_id=%s", pattern, user_id)

    except Exception:
        logger.exception("personality: ошибка emotional patterns")


def get_emotional_hint(user_id: int) -> str:
    """
    Если сегодня «стрессовый день» по паттерну — добавляет инструкцию.
    """
    try:

        patterns = get_memory_by_category(user_id, _MOOD_CATEGORY)
        if not patterns:
            return ""

        stress = patterns.get("стресс_паттерн", "")
        if not stress:
            return ""

        today = _DAY_NAMES[datetime.utcnow().weekday()]
        if today in stress:
            return (
                f"По наблюдениям, {today} — обычно напряжённый день для Сократа. "
                f"Будь чуть мягче и внимательнее чем обычно."
            )
        return ""
    except Exception:
        return ""


# ── Сборка всех подсказок для промпта ────────────────────────────────────────

# ── 5. Наблюдения + реакция на повторение ────────────────────────────────────

_REPEAT_THRESHOLD = 3


def get_observation_hint(user_id: int, message: str) -> str:
    """
    Два паттерна:
    - Реакция на повторение: тема встречается 3+ раз
    - Наблюдение по времени суток
    """
    hints = []
    msg_lower = message.lower()

    # Паттерн повторения — из interaction_memory
    try:
        from app.database import get_top_interactions
        for topic, _, freq in get_top_interactions(user_id, limit=5):
            if freq >= _REPEAT_THRESHOLD:
                topic_words = topic.lower().split()
                if sum(1 for w in topic_words if w in msg_lower) >= 1:
                    hints.append(
                        f"Сократ возвращается к теме «{topic}» уже {freq} раз. "
                        f"Если уместно — спроси что именно его в этом держит."
                    )
                    break
    except Exception:
        pass

    # Наблюдение: поздняя ночь
    try:
        now_hour = (datetime.utcnow().hour + 3) % 24
        if now_hour >= 22 or now_hour < 2:
            hints.append(
                "Сократ пишет поздно ночью — если разговор серьёзный, "
                "можно мягко это отметить."
            )
    except Exception:
        pass

    return "\n".join(hints)


# ── Сборка всех подсказок для промпта ────────────────────────────────────────

def build_personality_block(user_id: int, message: str) -> str:
    """
    Собирает все personality-подсказки в один блок для системного промпта.
    Вызывается синхронно из raya_agent.
    """
    hints = []

    for fn in [
        lambda: get_mirror_hint(message),
        lambda: get_feedback_hint(user_id),
        lambda: get_depth_hint(user_id),
        lambda: get_emotional_hint(user_id),
        lambda: get_observation_hint(user_id, message),
    ]:
        try:
            h = fn()
            if h:
                hints.append(h)
        except Exception:
            pass

    if not hints:
        return ""

    return "🧠 Подсказки по стилю:\n" + "\n".join(f"  • {h}" for h in hints)
