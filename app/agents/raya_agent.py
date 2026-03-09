"""
raya_agent.py — главный агент RaYa.
Fallback для общих разговоров, напоминания, полный эмоциональный интеллект.
"""
import asyncio
import logging
from datetime import datetime

from langchain_core.messages import HumanMessage, SystemMessage

from app.agents.base_agent import AgentContext, AgentResult, BaseAgent
from app.config import settings
from app.context_service import ContextService
from app.emotional_service import (
    detect_mood,
    detect_task_type,
    extract_emotion_tag,
    get_response_length_hint,
    mood_context,
)
from app.utils import build_reminder_prompt_block, clean_reminder_tag, parse_reminder

logger = logging.getLogger(__name__)


class RayaAgent(BaseAgent):
    agent_name = "raya"
    timeout    = 30

    def __init__(self) -> None:
        super().__init__()
        self._bg_tasks: set[asyncio.Task] = set()

    def _system_prompt(self) -> str:
        return settings.system_prompt

    def _run_background(self, coro) -> None:
        """Запускает корутину в фоне, защищая от GC."""
        task = asyncio.create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    async def _execute(self, ctx: AgentContext) -> AgentResult:
        now_utc   = datetime.utcnow()
        is_voice  = ctx.extra.get("is_voice", False)

        # ── 1. Трекинг настроения (фоново, защищён от GC) ────────────────────
        self._run_background(self._track_mood(ctx))

        # ── 2. Эмоциональный контекст из истории настроений ──────────────────
        from app.database import get_recent_moods
        moods    = get_recent_moods(ctx.user_id, limit=7)
        emot_ctx = mood_context(moods)

        # ── 2б. Контекст разговора ────────────────────────────────────────────
        conv_ctx = ContextService.get_prompt_block(ctx.user_id)

        # ── 3. Тип задачи → тон ──────────────────────────────────────────────
        task_type, expected_emotion, tone_hint = detect_task_type(ctx.message)

        # ── 4. Подсказка по длине ответа ─────────────────────────────────────
        length_hint = get_response_length_hint(ctx.message, is_voice=is_voice)

        # ── 5. Собираем системный промпт ─────────────────────────────────────
        system = settings.system_prompt

        if emot_ctx:
            system += f"\n\n{emot_ctx}"

        if conv_ctx:
            system += f"\n\n{conv_ctx}"

        # Структурированная память — богатый контекст вместо плоского списка
        from app.database import format_memory_for_prompt
        structured_ctx = format_memory_for_prompt(ctx.user_id)
        if structured_ctx:
            system += f"\n\n{structured_ctx}"
        elif ctx.memory_facts:
            # Fallback на старые факты если структурированной памяти нет
            facts   = "\n".join(f"- {f}" for f in ctx.memory_facts)
            system += f"\n\nЧто известно о Сократе:\n{facts}"

        if tone_hint:
            system += f"\n\n{tone_hint}"

        if length_hint:
            system += f"\n\n{length_hint}"

        system += build_reminder_prompt_block(now_utc)

        # ── 6. Сообщение + поисковый контекст ────────────────────────────────
        content = ctx.message
        if ctx.search_results:
            content = f"{ctx.message}\n\n[Контекст из поиска:]\n{ctx.search_results}"

        messages = [
            SystemMessage(content=system),
            *ctx.history,
            HumanMessage(content=content),
        ]

        # ── 7. Вызов модели ───────────────────────────────────────────────────
        response = await self._llm.ainvoke(messages)
        raw      = str(response.content)

        # ── 8. Извлекаем emotion tag и чистим ответ ──────────────────────────
        emotion, raw_clean = extract_emotion_tag(raw)

        reminder = parse_reminder(raw_clean, now_utc)
        reply    = clean_reminder_tag(raw_clean)

        logger.debug("emotion: %s | task: %s | user=%s", emotion, task_type, ctx.user_id)

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
        """Определяет настроение пользователя и сохраняет в mood_log фоново."""
        try:
            from app.database import save_mood
            mood = await detect_mood(ctx.message, self._llm)
            if mood != "нейтрально":
                save_mood(ctx.user_id, mood, ctx.message[:100])
                logger.debug("mood: %s | user=%s", mood, ctx.user_id)
        except Exception:
            pass
