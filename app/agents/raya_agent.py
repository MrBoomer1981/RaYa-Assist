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
from app.database import format_context_for_prompt
from app.personality_service import update_state, state_to_prompt
from app.personality_service import get_opinion_hint
from app.personality_service import (
    detect_mood,
    detect_task_type,
    extract_emotion_tag,
    get_response_length_hint,
    mood_context,
)
from app.utils import build_reminder_prompt_block, clean_reminder_tag, parse_reminder
from app.database import (
    get_recent_moods, format_interaction_memory,
    format_memory_for_prompt, save_mood,
)
from app.personality_service import build_observation_block
from app.personality_service import build_personality_block

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
        moods    = get_recent_moods(ctx.user_id, limit=7)
        emot_ctx = mood_context(moods)

        # ── 2б. Контекст разговора ────────────────────────────────────────────
        conv_ctx = format_context_for_prompt(ctx.user_id)

        # ── 2в. Personality block (mirroring, feedback, depth, emotional) ─────
        personality_ctx = build_personality_block(ctx.user_id, ctx.message)

        # ── 2г. Internal State ────────────────────────────────────────────────
        state     = update_state(ctx.user_id, ctx.message, ctx.search_results)
        state_ctx = state_to_prompt(state)

        # ── 2д. Personal Opinions ─────────────────────────────────────────────
        opinion_ctx = get_opinion_hint(ctx.message)

        # ── 2е. Interaction Memory ────────────────────────────────────────────
        interaction_ctx = format_interaction_memory(ctx.user_id)

        # ── 2ж. Живые наблюдения (повторы, паттерны поведения) ───────────────
        observation_ctx = build_observation_block(ctx.user_id, ctx.message)

        # ── 3. Тип задачи → тон ──────────────────────────────────────────────
        task_type, expected_emotion, tone_hint = detect_task_type(ctx.message)

        # ── 4. Подсказка по длине ответа ─────────────────────────────────────
        length_hint = get_response_length_hint(ctx.message, is_voice=is_voice)

        # ── 5. Собираем системный промпт ─────────────────────────────────────
        system = settings.system_prompt

        # Жёсткие правила — дублируем здесь, не зависят от persona.txt
        system += (
            "\n\nКРИТИЧНО — ПРАВИЛА КОТОРЫЕ НЕЛЬЗЯ НАРУШАТЬ:\n"
            "1. Обращайся к пользователю ТОЛЬКО 'Сократ'. "
            "Никогда не копируй обращение из его сообщения. "
            "Если он написал 'Рай' — это НЕ его имя, его имя всегда Сократ.\n"
            "2. Никогда не пиши 'согласно данным с X', 'по данным X', 'на сайте X'. "
            "Информацию из поиска излагай своими словами без упоминания источника.\n"
            "3. На вопросы о курсах/ценах — максимум 2-3 предложения. "
            "Только самое важное, без перечислений.\n"
            "4. Никаких URL в ответах."
        )

        if emot_ctx:
            system += f"\n\n{emot_ctx}"

        if conv_ctx:
            system += f"\n\n{conv_ctx}"

        if personality_ctx:
            system += f"\n\n{personality_ctx}"

        if state_ctx:
            system += f"\n\n{state_ctx}"

        if opinion_ctx:
            system += f"\n\n{opinion_ctx}"

        if interaction_ctx:
            system += f"\n\n{interaction_ctx}"

        # Принятые решения — не противоречим
        decisions_block = ctx.extra.get("decisions_block", "")
        if decisions_block:
            system += f"\n\n{decisions_block}"

        # Фраза-мостик: RaYa вплетает её в начало ответа естественно
        resume_bridge = ctx.extra.get("resume_bridge", "")
        if resume_bridge:
            system += (
                f"\n\nВАЖНО: Сократ вернулся после паузы. Начни ответ с естественного "
                f"упоминания того о чём говорили: '{resume_bridge}' — "
                f"вплети это органично, не как отдельный абзац."
            )

        # Структурированная память — богатый контекст вместо плоского списка
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

        # ── 7. Agentic loop с vault-инструментом ─────────────────────────────
        llm_with_tools = self._llm.bind_tools([VAULT_TOOL])
        response    = await llm_with_tools.ainvoke(messages)
        raw         = str(response.content) if response.content else ""
        tool_calls  = getattr(response, "tool_calls", []) or []
        vault_results = []

        # Обрабатываем tool_calls (параллельно если их несколько)
        if tool_calls:
            from langchain_core.messages import ToolMessage as _TM
            results = await process_tool_calls(tool_calls, ctx.user_id)
            vault_results = [r["result"] for r in results]

            # Строим ToolMessages для каждого вызова
            tool_messages = [
                _TM(content=r["result"], tool_call_id=r["tool_call_id"])
                for r in results
            ]

            # Финальный ответ модели с результатами инструментов
            final_msgs = messages + [response] + tool_messages
            final      = await self._llm.ainvoke(final_msgs)
            raw        = str(final.content)

            for r in results:
                logger.info("🔧 vault: %s", r["result"][:80])

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
                "vault_ops": vault_results,
            },
        )

    async def _track_mood(self, ctx: AgentContext) -> None:
        """Определяет настроение пользователя и сохраняет в mood_log фоново."""
        try:
            mood = await detect_mood(ctx.message, self._llm)
            if mood != "нейтрально":
                save_mood(ctx.user_id, mood, ctx.message[:100])
                logger.debug("mood: %s | user=%s", mood, ctx.user_id)
        except Exception:
            logger.debug("raya: mood save failed", exc_info=True)
