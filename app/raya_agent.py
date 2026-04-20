"""
raya_agent.py — главный агент RaYa.

Оптимизации v2:
- Параллельный сбор контекста через asyncio.gather
- Кэш системного промпта (TTL 60с) — не пересобирать на каждый запрос
- llm_with_tools создаётся один раз в __init__
- Сводка активных задач встроена в контекст
"""
import asyncio
import logging
import time
from datetime import datetime

from langchain_core.messages import HumanMessage, SystemMessage

from app.agents.base_agent import AgentContext, AgentResult, BaseAgent
from app.config import settings
from app.database import (
    format_context_for_prompt, format_interaction_memory,
    format_memory_for_prompt, get_recent_moods, save_mood, get_user_name,
)
from app.feature_flags import FEATURE_EMOTIONAL_SYSTEM, FEATURE_PERSONA_VERBOSE
from app.personality_service import (
    build_observation_block, build_personality_block,
    detect_mood, detect_task_type, extract_emotion_tag,
    get_opinion_hint, get_response_length_hint,
    mood_context, state_to_prompt, update_state,
)
from app.utils import build_reminder_prompt_block, clean_reminder_tag, parse_reminder

logger = logging.getLogger(__name__)

# Жёсткие правила — дописываются к persona.txt
def _build_hard_rules(user_name: str, user_id: int = 0) -> str:
    """Жёсткие правила с учётом персональных настроек пользователя."""
    from app.user_settings import get_settings
    s = get_settings(user_id) if user_id else None

    lang_rule = (
        "IMPORTANT: Always respond in ENGLISH.\n"
        if s and s.language == "en" else ""
    )
    length_rule = {
        "short":  "Длина: 1-2 предложения — только суть.",
        "medium": "Длина: до 7 предложений — баланс краткости и полноты.",
        "long":   "Длина: подробно, используй структуру если нужно.",
    }.get(s.response_length if s else "medium", "")

    style_rule = {
        "friendly": "Тон: живой, дружеский.",
        "formal":   "Тон: профессиональный, без фамильярности.",
        "concise":  "Тон: максимально лаконичный, без вводных фраз.",
    }.get(s.response_style if s else "friendly", "")

    return (
        "\n\nКРИТИЧНО — ПРАВИЛА:\n"
        + lang_rule
        + f"1. Обращайся по имени '{user_name}'.\n"
        "2. Никогда не пиши 'согласно данным с X', 'на сайте X'. "
        "Данные из поиска — своими словами.\n"
        "3. Никаких URL в ответах.\n"
        f"4. {length_rule}\n"
        f"5. {style_rule}"
    )


def _build_date_block(now_utc: datetime) -> str:
    """
    Явно сообщает LLM текущую дату и время.
    Без этого модель может опираться на данные из обучения
    вместо свежих результатов поиска.
    """
    msk = now_utc.hour + 3  # МСК = UTC+3
    days_ru = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
    months_ru = ["января", "февраля", "марта", "апреля", "мая", "июня",
                 "июля", "августа", "сентября", "октября", "ноября", "декабря"]
    day_name = days_ru[now_utc.weekday()]
    month    = months_ru[now_utc.month - 1]
    return (
        f"📅 Сейчас: {day_name}, {now_utc.day} {month} {now_utc.year} г., "
        f"{msk % 24:02d}:{now_utc.minute:02d} МСК.\n"
        "Используй эту дату как точку отсчёта. Если в поиске есть более свежие данные — "
        "доверяй им, а не своим знаниям из обучения."
    )

_SYSTEM_CACHE_TTL = 60  # секунд — как долго кэшируем статичную часть промпта


class RayaAgent(BaseAgent):
    agent_name = "raya"
    timeout    = 30

    def __init__(self) -> None:
        super().__init__()
        self._bg_tasks: set[asyncio.Task] = set()
        # Создаём llm_with_tools один раз — не bind_tools на каждый запрос
        # Кэш системного промпта per-user (user_id -> (prompt, timestamp))
        self._prompt_cache: dict[int, tuple[str, float]] = {}

    def _system_prompt(self) -> str:
        return settings.system_prompt

    def _run_background(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    def _get_static_prompt(self, user_id: int = 0) -> str:
        """Системный промпт с именем — кэшируется per-user на TTL секунд."""
        now = time.monotonic()
        cached = self._prompt_cache.get(user_id)
        if cached and (now - cached[1]) < _SYSTEM_CACHE_TTL:
            return cached[0]
        user_name = get_user_name(user_id) if user_id else "друг"
        prompt = settings.system_prompt + _build_hard_rules(user_name, ctx.user_id)
        self._prompt_cache[user_id] = (prompt, now)
        # Не даём кэшу расти бесконечно
        if len(self._prompt_cache) > 200:
            oldest = min(self._prompt_cache, key=lambda k: self._prompt_cache[k][1])
            del self._prompt_cache[oldest]
        return prompt

    async def _execute(self, ctx: AgentContext) -> AgentResult:
        now_utc  = datetime.utcnow()
        is_voice = ctx.extra.get("is_voice", False)

        # ── 1. Фоновый трекинг настроения ────────────────────────────────────
        if FEATURE_EMOTIONAL_SYSTEM:
            self._run_background(self._track_mood(ctx))

        # ── 2. Параллельный сбор контекста ───────────────────────────────────
        async def _get_moods():
            if not FEATURE_EMOTIONAL_SYSTEM:
                return ""
            moods = get_recent_moods(ctx.user_id, limit=7)
            return mood_context(moods)

        async def _get_conv():
            return format_context_for_prompt(ctx.user_id)

        async def _get_personality():
            if not FEATURE_PERSONA_VERBOSE:
                return ""
            return build_personality_block(ctx.user_id, ctx.message)

        async def _get_state():
            state = update_state(ctx.user_id, ctx.message, ctx.search_results)
            return state_to_prompt(state)

        async def _get_interaction():
            return format_interaction_memory(ctx.user_id)

        async def _get_observation():
            return build_observation_block(ctx.user_id, ctx.message)

        async def _get_memory():
            structured = format_memory_for_prompt(ctx.user_id)
            if structured:
                return structured
            if ctx.memory_facts:
                facts = "\n".join(f"- {f}" for f in ctx.memory_facts)
                return f"Что известно о {get_user_name(ctx.user_id)}:\n{facts}"
            return ""

        async def _get_tasks_summary():
            try:
                from app.database import get_active_tasks
                tasks = get_active_tasks(ctx.user_id)
                if not tasks:
                    return ""
                emoji = {1: "🔴", 2: "🟡", 3: "🟠"}
                urgent = [t for t in tasks if t[2] == 1]
                total  = len(tasks)
                parts  = []
                if urgent:
                    parts.append("🔴 Срочные: " + "; ".join(t[1] for t in urgent[:3]))
                parts.append(f"Всего задач: {total}")
                return "\n".join(parts)
            except Exception:
                return ""

        # Запускаем всё параллельно
        (
            emot_ctx, conv_ctx, personality_ctx, state_ctx,
            interaction_ctx, observation_ctx, memory_ctx, tasks_summary
        ) = await asyncio.gather(
            _get_moods(), _get_conv(), _get_personality(), _get_state(),
            _get_interaction(), _get_observation(), _get_memory(), _get_tasks_summary()
        )

        # ── 3. Синхронные вычисления (быстрые) ───────────────────────────────
        opinion_ctx = get_opinion_hint(ctx.message)
        task_type, _, tone_hint = detect_task_type(ctx.message)
        length_hint = get_response_length_hint(ctx.message, is_voice=is_voice)

        # ── 4. Собираем системный промпт ─────────────────────────────────────
        system = self._get_static_prompt(ctx.user_id)

        for block in filter(None, [
            emot_ctx, conv_ctx, personality_ctx, state_ctx,
            opinion_ctx, interaction_ctx, observation_ctx, memory_ctx,
            tasks_summary, tone_hint, length_hint,
        ]):
            system += f"\n\n{block}"

        # Динамические блоки из extra
        decisions = ctx.extra.get("decisions_block", "")
        if decisions:
            system += f"\n\n{decisions}"

        resume = ctx.extra.get("resume_bridge", "")
        if resume:
            system += (
                f"\n\nВАЖНО: {get_user_name(ctx.user_id)} вернулся после паузы. Начни ответ с естественного "
                f"упоминания того о чём говорили: '{resume}' — "
                f"вплети это органично, не как отдельный абзац."
            )

        system += build_reminder_prompt_block(now_utc)

        # Текущая дата — критично для свежести ответов
        system += f"\n\n{_build_date_block(now_utc)}"

        # ── 5. Сообщение ──────────────────────────────────────────────────────
        content = ctx.message
        if ctx.search_results:
            # Если результаты уже содержат метку времени (от нашего SearchService) —
            # не дублируем. Иначе добавляем пометку что данные актуальны.
            search_block = ctx.search_results
            if "[Данные получены:" not in search_block:
                search_block = (
                    f"[Данные из интернета, получены только что — используй их как актуальные]\n"
                    f"{search_block}"
                )
            content = f"{ctx.message}\n\n[Актуальная информация из поиска:]\n{search_block}"

        messages = [
            SystemMessage(content=system),
            *ctx.history,
            HumanMessage(content=content),
        ]

        # ── 6. Вызов модели ───────────────────────────────────────────────────
        response = await self._llm.ainvoke(messages)
        raw      = str(response.content) if response.content else ""

        # ── 7. Постобработка ──────────────────────────────────────────────────
        emotion, raw_clean = extract_emotion_tag(raw)
        reminder = parse_reminder(raw_clean, now_utc)
        reply    = clean_reminder_tag(raw_clean)

        logger.debug("emotion=%s task=%s user=%s", emotion, task_type, ctx.user_id)

        return AgentResult(
            success=True,
            content=reply,
            agent_name=self.agent_name,
            needs_critic=False,
            metadata={
                "reminder":  reminder,
                "emotion":   emotion,
                "task_type": task_type,
            },
        )

    async def _track_mood(self, ctx: AgentContext) -> None:
        try:
            mood = await detect_mood(ctx.message, self._llm)
            if mood != "нейтрально":
                save_mood(ctx.user_id, mood, ctx.message[:100])
        except Exception:
            logger.debug("mood tracking failed", exc_info=True)
