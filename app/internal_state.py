"""
internal_state.py — Internal State (внутреннее состояние RaYa).

У RaYa есть четыре параметра состояния которые меняются во время общения:
  mood        — настроение (neutral / curious / warm / playful / concerned / tired)
  energy      — уровень энергии (1-5)
  interest    — интерес к текущей теме (1-5)
  confidence  — уверенность в ответе (1-5)

Параметры влияют на промпт — создают ощущение живой реакции.
Хранятся в памяти процесса (не в БД — состояние сессионное).
"""
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime

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


def get_state(user_id: int) -> RaYaState:
    if user_id not in _states:
        _states[user_id] = RaYaState()
    return _states[user_id]


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
        "concerned": "Сократ звучит напряжённо — будь особенно внимательной и поддерживающей.",
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
