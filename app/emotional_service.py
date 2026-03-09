"""
emotional_service.py — эмоциональный интеллект RaYa.

Функции:
- detect_mood()              — настроение пользователя по тексту
- mood_context()             — эмоциональный контекст для промпта
- detect_task_type()         — тип задачи → инструкция по тону
- extract_emotion_tag()      — извлекает <emotion> тег из ответа модели
- get_response_length_hint() — подсказка по длине ответа
- get_last_message_time()    — время последнего сообщения
- generate_initiative_message() — RaYa пишет первой
"""
import itertools as _itertools
import logging
import re
from datetime import datetime

logger = logging.getLogger(__name__)

# ── Настроения пользователя ───────────────────────────────────────────────────

MOODS = [
    "радость", "энтузиазм", "спокойствие",
    "усталость", "стресс", "грусть",
    "раздражение", "скука", "тревога", "нейтрально",
]

_DETECT_PROMPT = """\
Определи настроение человека по его сообщению. Одно слово из списка:
радость, энтузиазм, спокойствие, усталость, стресс, грусть, раздражение, скука, тревога, нейтрально.

Сообщение: {message}

Ответь ТОЛЬКО одним словом из списка."""


async def detect_mood(message: str, llm) -> str:
    """Определяет настроение пользователя. Возвращает слово из MOODS."""
    if len(message.strip()) < 5:
        return "нейтрально"
    try:
        from langchain_core.messages import HumanMessage
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

    lines = [f"Последние настроения Сократа: {', '.join(mood_list)}."]

    if neg_count >= 3:
        lines.append(
            "Последние дни он чаще в негативном состоянии — "
            "будь внимательнее, не грузи лишним, можешь мягко спросить как дела."
        )
    elif pos_count >= 3:
        lines.append("Сократ в хорошем настроении — можно быть энергичнее и живее.")
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

def get_last_message_time(user_id: int) -> datetime | None:
    """Возвращает время последнего сообщения пользователя."""
    try:
        from app.database import _conn
        with _conn() as con:
            row = con.execute("""
                SELECT created_at FROM history
                WHERE user_id = ? AND role = 'human'
                ORDER BY created_at DESC LIMIT 1
            """, (user_id,)).fetchone()
        if row:
            return datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S")
        return None
    except Exception:
        logger.exception("get_last_message_time: ошибка")
        return None


_INITIATIVE_PROMPTS = [
    "Сократ давно не писал. Напиши ему короткое живое сообщение — спроси как дела, "
    "поделись чем-то интересным из мира технологий или просто дай знать что ты здесь. "
    "Максимум 2-3 предложения. Без формальностей.",

    "Сократ долго молчит. Напиши ему что-нибудь — короткую мысль, интересный факт "
    "или просто напомни что ты рядом. Живо и по-человечески, 1-2 предложения.",

    "Сократ давно не выходил на связь. Напиши тёплое короткое сообщение. "
    "Можешь упомянуть что-то из его последних разговоров или задач. 1-2 предложения.",
]

_initiative_cycle = _itertools.cycle(_INITIATIVE_PROMPTS)


async def generate_initiative_message(user_id: int, llm) -> str:
    """Генерирует инициативное сообщение от RaYa."""
    try:
        from langchain_core.messages import HumanMessage, SystemMessage
        from app.database import load_memory, get_active_tasks

        facts  = load_memory(user_id)
        tasks  = get_active_tasks(user_id)

        context = ""
        if facts:
            context += f"Что RaYa знает о Сократе: {'; '.join(facts[:3])}\n"
        if tasks:
            context += f"Его активные задачи: {', '.join(t[1] for t in tasks[:2])}\n"

        prompt = next(_initiative_cycle)

        response = await llm.ainvoke([
            SystemMessage(content="Ты RaYa — личный ассистент и друг Сократа. Обращайся только 'Сократ'."),
            HumanMessage(content=prompt + "\n\n" + context),
        ])
        return str(response.content).strip()

    except Exception:
        logger.exception("generate_initiative_message: ошибка")
        return "Сократ, давно не слышала тебя. Всё хорошо?"
