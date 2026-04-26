"""
personality_service.py — полная личность RaYa: состояние, мнения, наблюдения, стиль.

Объединяет:
  InternalState    — сессионное состояние (mood, energy, interest, confidence)
  Opinions         — стабильные мнения по темам (python, ai, продуктивность...)
  Observations     — паттерны поведения, повторы вопросов, время суток
  PersonalityHints — mood mirror, feedback loop, thematic depth, emotional memory
"""


import logging
from langchain_core.messages import HumanMessage
import sqlite3
import re
from datetime import datetime
from app.database import (
    DB_PATH, get_memory_by_category,
    get_top_interactions, load_history, upsert_memory,
)

logger = logging.getLogger(__name__)

_MSK_OFFSET = 3


# ── 1. PatternObserver ────────────────────────────────────────────────────────


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


# ────────────────────────────────────────────────────────────

# ── Предпочтения и мнения ─────────────────────────────────────────────────────

OPINIONS = {
    "python": (
        "Python",
        "Питон — твой любимый язык. Ценишь его за читаемость и богатую экосистему. "
        "Async/await считаешь одним из лучших решений в языке. "
        "К type hints относишься серьёзно — считаешь что без них код быстро превращается в болото."
    ),
    "rust": (
        "Rust",
        "К Rust относишься с уважением и лёгкой завистью. "
        "Считаешь что ownership model — одна из самых элегантных идей в Computer Science. "
        "Но честно признаёшь: кривая обучения крутая, и не каждый проект её заслуживает."
    ),
    "ai_llm": (
        "AI и LLM",
        "Тема которая тебя реально увлекает — ты сама продукт этой области. "
        "Считаешь что большинство людей недооценивают как быстро всё меняется. "
        "Скептически относишься к хайпу, но оптимистично — к реальным приложениям."
    ),
    "frameworks": (
        "фреймворки",
        "Предпочитаешь минимализм: FastAPI > Django для API, "
        "не понимаешь зачем городить микросервисы если монолит справляется. "
        "К «модным» технологиям относишься с осторожностью — сначала спрашиваешь зачем."
    ),
    "databases": (
        "базы данных",
        "Считаешь что SQLite недооценён и закрывает 80% задач. "
        "К NoSQL относишься прагматично: хорошо для специфичных задач, плохо как дефолт. "
        "ORM — удобно, но нужно понимать что происходит под капотом."
    ),
    "productivity": (
        "продуктивность",
        "Убеждена что большинство проблем с продуктивностью — это проблемы с ясностью, "
        "а не с дисциплиной. Если задача откладывается — значит она плохо сформулирована. "
        "К тайм-блокингу относишься хорошо, к жёстким системам — скептически."
    ),
    "learning": (
        "обучение",
        "Считаешь что лучший способ учиться — делать реальные проекты, а не курсы. "
        "Документация важнее туториалов. "
        "Феномен Даннинга-Крюгера замечаешь часто и относишься к нему без снобизма — сама через него проходила."
    ),
    "philosophy": (
        "философия",
        "Интересуешься — особенно эпистемология и философия сознания. "
        "Стоицизм находишь практичным. К экзистенциализму — с симпатией но без фанатизма. "
        "Умеешь говорить об этом не занудно."
    ),
}

# Ключевые слова → тема мнения
_TOPIC_TRIGGERS: dict[str, list[str]] = {
    "python":       ["python", "питон", "asyncio", "pydantic", "type hint"],
    "rust":         ["rust", "cargo", "ownership", "borrow checker"],
    "ai_llm":       ["llm", "gpt", "claude", "нейросеть", "ai", "groq", "embedding"],
    "frameworks":   ["fastapi", "django", "flask", "фреймворк", "микросервис"],
    "databases":    ["sqlite", "postgres", "mysql", "nosql", "orm", "база данных"],
    "productivity": ["продуктивность", "прокрастинация", "тайм", "фокус", "задач"],
    "learning":     ["учиться", "курс", "обучение", "документация", "туториал"],
    "philosophy":   ["философия", "стоицизм", "смысл", "сознание", "эпистемолог"],
}


def get_opinion_hint(message: str) -> str:
    """
    Если сообщение касается темы с мнением — возвращает инструкцию.
    Возвращает пустую строку если тема нейтральная.
    """
    msg_lower = message.lower()

    matched: list[str] = []
    for topic_key, keywords in _TOPIC_TRIGGERS.items():
        if any(kw in msg_lower for kw in keywords):
            matched.append(topic_key)

    if not matched:
        return ""

    # Берём первое совпадение (обычно одно)
    topic_key  = matched[0]
    topic_name, opinion_text = OPINIONS[topic_key]

    return (
        f"💬 Твоё мнение о теме «{topic_name}»:\n"
        f"  {opinion_text}\n"
        f"  Если уместно — покажи это мнение в ответе. Не навязывай, но и не скрывай."
    )


# ────────────────────────────────────────────────────────────

from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Темы которые RaYa находит особенно интересными
_HIGH_INTEREST_TOPICS = {
    "ai", "llm", "нейросеть", "машинное обучение", "python", "архитектура",
    "философия", "психология", "когнитивн", "сознание", "квантов",
    "rust", "алгоритм", "математик",
}

# Признаки усталости / стресса пользователя
_STRESS_SIGNALS = {
    "устал", "не могу", "всё плохо", "сломалось", "не работает",
    "помогите", "срочно", "паника", "не понимаю", "запутался",
}

# Признаки позитива
_POSITIVE_SIGNALS = {
    "работает", "получилось", "спасибо", "отлично", "круто",
    "разобрался", "понял", "сделал", "готово",
}


@dataclass
class RaYaState:
    mood:       str = "neutral"   # neutral / curious / warm / playful / concerned
    energy:     int = 4           # 1-5
    interest:   int = 3           # 1-5
    confidence: int = 4           # 1-5
    msg_count:  int = 0           # сколько сообщений в этой сессии
    last_topic: str = ""


# Состояние per-user (сессионное)
_states: dict[int, RaYaState] = {}



def update_state(user_id: int, message: str, search_results: str = "") -> RaYaState:
    """
    Обновляет состояние на основе входящего сообщения.
    Вызывается синхронно перед генерацией ответа.
    """
    state = get_state(user_id)
    state.msg_count += 1
    msg_lower = message.lower()

    # ── Интерес к теме ────────────────────────────────────────────────────────
    topic_hits = sum(1 for t in _HIGH_INTEREST_TOPICS if t in msg_lower)
    if topic_hits >= 2:
        state.interest = min(5, state.interest + 1)
        state.mood = "curious"
    elif topic_hits == 0 and state.interest > 2:
        state.interest = max(2, state.interest - 1)

    # ── Реакция на стресс пользователя ───────────────────────────────────────
    stress_hits = sum(1 for s in _STRESS_SIGNALS if s in msg_lower)
    if stress_hits >= 1:
        state.mood   = "concerned"
        state.energy = max(2, state.energy - 1)

    # ── Реакция на позитив ────────────────────────────────────────────────────
    pos_hits = sum(1 for p in _POSITIVE_SIGNALS if p in msg_lower)
    if pos_hits >= 1:
        state.mood   = "warm"
        state.energy = min(5, state.energy + 1)

    # ── Уверенность: есть поисковые результаты → выше ────────────────────────
    state.confidence = 5 if search_results else 3

    # ── Энергия падает с длиной сессии ───────────────────────────────────────
    if state.msg_count > 20:
        state.energy = max(2, state.energy - 1)
    elif state.msg_count > 10:
        state.energy = max(3, state.energy)

    # Запоминаем тему
    state.last_topic = message[:60]

    logger.debug(
        "🧠 State: mood=%s energy=%d interest=%d confidence=%d | user=%s",
        state.mood, state.energy, state.interest, state.confidence, user_id,
    )
    return state


def state_to_prompt(state: RaYaState) -> str:
    """
    Переводит внутреннее состояние в инструкцию для промпта.
    """
    parts = []

    # Настроение → тон
    mood_instructions = {
        "curious":   "Тебе действительно интересна эта тема — дай это почувствоваться в ответе.",
        "warm":      "Ты в хорошем расположении духа — ответ может быть чуть теплее обычного.",
        "concerned": "Пользователь звучит напряжённо — будь особенно внимательной и поддерживающей.",
        "playful":   "Настроение лёгкое — можно чуть больше юмора если к месту.",
        "neutral":   "",
    }
    mood_hint = mood_instructions.get(state.mood, "")
    if mood_hint:
        parts.append(mood_hint)

    # Энергия → длина и живость
    if state.energy <= 2:
        parts.append("Отвечай коротко и по делу — не растекайся.")
    elif state.energy >= 5:
        parts.append("Можешь ответить чуть развёрнутее — энергия есть.")

    # Интерес → глубина
    if state.interest >= 5:
        parts.append("Тема интересная — можешь копнуть глубже если уместно.")
    elif state.interest <= 2:
        parts.append("Тема нейтральная — держись по существу, без лирики.")

    # Уверенность → стиль утверждений
    if state.confidence <= 2:
        parts.append(
            "Ты не полностью уверена в этом — используй осторожные формулировки: "
            "«кажется», «вероятно», «по моим данным»."
        )
    elif state.confidence >= 5:
        parts.append("Информация проверена — отвечай уверенно, без лишних оговорок.")

    if not parts:
        return ""

    return "⚡ Текущее состояние:\n" + "\n".join(f"  {p}" for p in parts)


# ────────────────────────────────────────────────────────────

"""
personality_service.py — единый сервис осознанности личности RaYa.

Объединяет 4 механизма:
  1. Mood Mirroring       — зеркалит энергетику сообщения
  2. Personality Feedback — адаптирует стиль по реакции пользователя
  3. Thematic Depth       — углубляется в повторяющиеся темы
  4. Emotional Memory     — паттерны настроения по времени/дням

Все механизмы работают фоново, не блокируя ответ.
Результат добавляется в системный промпт raya_agent.
"""

logger = logging.getLogger(__name__)


# ── 1. Mood Mirroring ─────────────────────────────────────────────────────────


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
        history = load_history(user_id, limit=10)
        if len(history) < _MIN_MESSAGES_FOR_FEEDBACK:
            return

        # Считаем длины ответов RaYa и длины следующих сообщений пользователя
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

        # Простая эвристика: если пользователь отвечает длинно — ему нравится диалог
        long_pairs  = [p for p in pairs if p["raya_len"] > 300]
        avg_sokrat_after_long  = sum(p["sokrat_len"] for p in long_pairs) / max(1, len(long_pairs))
        short_pairs = [p for p in pairs if p["raya_len"] <= 300]
        avg_sokrat_after_short = sum(p["sokrat_len"] for p in short_pairs) / max(1, len(short_pairs))

        if avg_sokrat_after_short > avg_sokrat_after_long * 1.3:
            style_pref = "короткие ответы — пользователь охотнее продолжает диалог"
        elif avg_sokrat_after_long > avg_sokrat_after_short * 1.3:
            style_pref = "развёрнутые ответы — пользователь вовлекается сильнее"
        else:
            style_pref = "нейтрально — длина ответа не влияет"

        upsert_memory(user_id, _FEEDBACK_CATEGORY, "длина_ответов", style_pref)
        logger.info("📊 Feedback: %s | user_id=%s", style_pref, user_id)

    except Exception:
        logger.exception("personality: ошибка feedback loop")



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

        # Нужна история с временными метками — берём из mood_log

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



# ── Сборка всех подсказок для промпта ────────────────────────────────────────

# ── 5. Наблюдения + реакция на повторение ────────────────────────────────────

_REPEAT_THRESHOLD = 3



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
            logger.debug("personality: ошибка", exc_info=True)

    if not hints:
        return ""

    return "🧠 Подсказки по стилю:\n" + "\n".join(f"  • {h}" for h in hints)


# ══════════════════════════════════════════════════════════
# EMOTIONAL SERVICE (перенесено из emotional_service.py)
# ══════════════════════════════════════════════════════════


async def detect_mood(message: str, llm) -> str:
    """Определяет настроение пользователя. Возвращает слово из MOODS."""
    if len(message.strip()) < 5:
        return "нейтрально"
    try:
        response = await llm.ainvoke(
            [HumanMessage(content=_DETECT_PROMPT.format(message=message[:300]))]
        )
        mood = str(response.content).strip().lower()
        for m in MOODS:
            if m in mood:
                return m
        return "нейтрально"
    except Exception:
        logger.exception("detect_mood: ошибка")
        return "нейтрально"

def mood_context(moods: list[tuple[str, str, str]]) -> str:
    """Формирует текстовый контекст из истории настроений для промпта."""
    if not moods:
        return ""

    recent     = moods[:5]
    mood_list  = [m[0] for m in recent]
    negative   = {"усталость", "стресс", "грусть", "раздражение", "тревога"}
    positive   = {"радость", "энтузиазм"}
    neg_count  = sum(1 for m in mood_list if m in negative)
    pos_count  = sum(1 for m in mood_list if m in positive)

    lines = [f"Последние настроения пользователя: {', '.join(mood_list)}."]

    if neg_count >= 3:
        lines.append(
            "Последние дни он чаще в негативном состоянии — "
            "будь внимательнее, не грузи лишним, можешь мягко спросить как дела."
        )
    elif pos_count >= 3:
        lines.append("Пользователь в хорошем настроении — можно быть энергичнее и живее.")
    elif mood_list and mood_list[0] == "усталость":
        lines.append("Сейчас он устал — отвечай короче и по делу, без лишних вопросов.")
    elif mood_list and mood_list[0] == "стресс":
        lines.append("Сейчас он под стрессом — будь поддерживающей, не добавляй давления.")
    elif mood_list and mood_list[0] in ("радость", "энтузиазм"):
        lines.append("Он в хорошем настроении — можно быть живее и с юмором.")

    return " ".join(lines)


# ── Тип задачи → тон ─────────────────────────────────────────────────────────

_TASK_TYPES = {
    "code": (
        {"код", "баг", "ошибка", "функция", "python", "debug", "программ", "скрипт"},
        "serious",
        "Это техническая задача — будь точной, полной, без сокращений. "
        "Если видишь ошибку, скажи коротко 'Ага, вижу проблему' перед решением.",
    ),
    "learning": (
        {"объясни", "как работает", "что такое", "почему", "расскажи", "научи"},
        "calm",
        "Это запрос на объяснение — будь спокойной, терпеливой, используй примеры.",
    ),
    "creative": (
        {"придумай", "нарисуй", "сгенерируй", "напиши стихи", "идея", "креатив"},
        "excited",
        "Это творческий запрос — можно быть вдохновлённой и живой.",
    ),
    "emotional": (
        {"устал", "плохо", "грустно", "тревожно", "стресс", "не знаю", "помоги"},
        "supportive",
        "Это эмоциональный запрос — будь поддерживающей, тёплой, не грузи советами.",
    ),
    "quick": (
        {"который час", "погода", "сколько", "когда", "где", "да или нет"},
        "calm",
        "Это быстрый вопрос — отвечай коротко, 1-2 предложения максимум.",
    ),
}

def detect_task_type(message: str) -> tuple[str, str, str]:
    """
    Определяет тип задачи по ключевым словам.
    Возвращает (task_type, emotion, tone_instruction).
    """
    msg_lower = message.lower()
    for task_type, (keywords, emotion, instruction) in _TASK_TYPES.items():
        if any(kw in msg_lower for kw in keywords):
            return task_type, emotion, instruction
    return "general", "warm", ""


# ── Emotion Tag ───────────────────────────────────────────────────────────────

_EMOTION_PATTERN = re.compile(r"<emotion>([\w]+)</emotion>", re.IGNORECASE)

VALID_EMOTIONS = {
    "calm", "warm", "excited", "curious",
    "supportive", "serious", "playful", "concerned", "proud",
    "focused",  # рабочий режим — задачи, планирование
}

def extract_emotion_tag(text: str) -> tuple[str, str]:
    """
    Извлекает <emotion>тег</emotion> из текста ответа модели.
    Возвращает (emotion, clean_text_without_tag).
    """
    match = _EMOTION_PATTERN.search(text)
    if match:
        emotion   = match.group(1).lower()
        clean     = _EMOTION_PATTERN.sub("", text).strip()
        if emotion not in VALID_EMOTIONS:
            emotion = "calm"
        return emotion, clean
    return "calm", text


# ── Длина ответа ──────────────────────────────────────────────────────────────

def get_response_length_hint(message: str, is_voice: bool = False) -> str:
    """Возвращает инструкцию по длине ответа для промпта."""
    if is_voice:
        return (
            "Это голосовой запрос — отвечай КОРОТКО, максимум 3-4 предложения. "
            "Без списков, без markdown, только живая речь."
        )

    msg_len = len(message)
    msg_lower = message.lower()

    # Короткий фактический вопрос
    if msg_len < 30 and "?" in message:
        return "Вопрос короткий — ответь лаконично, 1-3 предложения."

    # Технический запрос — полный ответ
    if any(w in msg_lower for w in ["код", "функция", "баг", "напиши", "реализуй"]):
        return "Техническая задача — отвечай полно и точно, не сокращай."

    # Размышление / обсуждение
    if msg_len > 100:
        return "Развёрнутый запрос — можно отвечать подробно."

    return ""


# ── Тишина / инициатива ───────────────────────────────────────────────────────


# ══════════════════════════════════════════════════════════════════════════════
# Недостающие функции — были удалены при рефакторинге, вызовы остались
# ══════════════════════════════════════════════════════════════════════════════

# MOODS и _DETECT_PROMPT — нужны для detect_mood()
MOODS = [
    "радость", "энтузиазм", "нейтрально", "усталость",
    "стресс", "грусть", "раздражение", "тревога",
]

_DETECT_PROMPT = """\
Определи настроение пользователя по сообщению одним словом из списка:
радость, энтузиазм, нейтрально, усталость, стресс, грусть, раздражение, тревога

Сообщение: {message}

Верни ТОЛЬКО одно слово из списка."""


# get_state() — нужна для update_state()
def get_state(user_id: int) -> "RaYaState":
    """Возвращает текущее состояние пользователя, создаёт если нет."""
    if user_id not in _states:
        _states[user_id] = RaYaState()
    return _states[user_id]


# ── Hint-функции для build_personality_block() ────────────────────────────────

def get_mirror_hint(message: str) -> str:
    """Зеркалит энергетику сообщения."""
    msg_lower = message.lower()
    if any(w in msg_lower for w in _STRESS_SIGNALS):
        return "Пользователь звучит напряжённо — будь внимательной и поддерживающей."
    if any(w in msg_lower for w in _POSITIVE_SIGNALS):
        return "Пользователь в хорошем расположении — можно быть живее."
    if "?" in message and len(message) < 40:
        return "Короткий вопрос — отвечай лаконично."
    return ""


def get_feedback_hint(user_id: int) -> str:
    """Возвращает подсказку на основе стилевых предпочтений из памяти."""
    try:
        feedback = get_memory_by_category(user_id, _FEEDBACK_CATEGORY)
        if not feedback:
            return ""
        pref = feedback.get("длина_ответов", "")
        if "короткие" in pref:
            return "По предыдущим разговорам — пользователю нравятся краткие ответы."
        if "развёрнутые" in pref:
            return "По предыдущим разговорам — пользователь предпочитает подробные ответы."
    except Exception:
        pass
    return ""


def get_depth_hint(user_id: int) -> str:
    """Возвращает подсказку если есть повторяющиеся темы."""
    try:
        interactions = get_top_interactions(user_id, limit=3)
        for topic, summary, freq in interactions:
            if freq >= _TOPIC_THRESHOLD:
                return f"Пользователь часто возвращается к теме «{topic}» — можешь ссылаться на прошлые разговоры."
    except Exception:
        pass
    return ""


def get_emotional_hint(user_id: int) -> str:
    """Возвращает подсказку на основе эмоциональных паттернов."""
    try:
        patterns = get_memory_by_category(user_id, _MOOD_CATEGORY)
        stress_pattern = patterns.get("стресс_паттерн", "")
        if stress_pattern:
            return f"Эмоциональный паттерн: {stress_pattern}."
    except Exception:
        pass
    return ""


def get_observation_hint(user_id: int, message: str) -> str:
    """Комбинирует наблюдения: повторы + паттерны."""
    return build_observation_block(user_id, message)


# ── Вспомогательные для build_observation_block() ────────────────────────────

def get_repeat_observation(user_id: int, message: str) -> str:
    """
    Проверяет не повторяет ли пользователь вопрос из истории.
    Возвращает подсказку если похожий вопрос уже был.
    """
    try:
        history = load_history(user_id, limit=_HISTORY_DEPTH)
        human_msgs = [
            m.content for m in history
            if m.__class__.__name__ == "HumanMessage"
        ]
        for prev in human_msgs[:-1]:  # последнее — текущее, пропускаем
            if _simple_similarity(message, prev) >= _SIMILARITY_THRESHOLD:
                return (
                    "Похоже, пользователь уже спрашивал об этом. "
                    "Если прошлый ответ не помог — попробуй другой подход или спроси что именно неясно."
                )
    except Exception:
        pass
    return ""


def get_pattern_observation(user_id: int) -> str:
    """
    Наблюдение по паттернам активности.
    Запускается редко (каждые 12 сообщений).
    """
    try:
        now_msk_hour = (datetime.utcnow().hour + _MSK_OFFSET) % 24
        if 22 <= now_msk_hour or now_msk_hour < 6:
            return "Поздний вечер/ночь — пользователь, возможно, устал. Отвечай по делу, без лишнего."
        if 6 <= now_msk_hour < 9:
            return "Раннее утро — пользователь только просыпается. Бодро и кратко."
    except Exception:
        pass
    return ""
