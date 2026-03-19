"""
raya_agent.py — главный агент RaYa.

Оптимизации v2:
- Параллельный сбор контекста через asyncio.gather
- Кэш системного промпта (TTL 60с) — не пересобирать на каждый запрос
- llm_with_tools создаётся один раз в __init__
- Vault tasks summary встроен в контекст
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
    format_memory_for_prompt, get_recent_moods, save_mood,
)
from app.feature_flags import FEATURE_EMOTIONAL_SYSTEM, FEATURE_PERSONA_VERBOSE
from app.personality_service import (
    build_observation_block, build_personality_block,
    detect_mood, detect_task_type, extract_emotion_tag,
    get_opinion_hint, get_response_length_hint,
    mood_context, state_to_prompt, update_state,
)
from app.utils import build_reminder_prompt_block, clean_reminder_tag, parse_reminder
from app.vault_tool import VAULT_TOOL, process_tool_calls

logger = logging.getLogger(__name__)

# Жёсткие правила — дописываются к persona.txt
_HARD_RULES = (
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

_SYSTEM_CACHE_TTL = 60  # секунд — как долго кэшируем статичную часть промпта


class RayaAgent(BaseAgent):
    agent_name = "raya"
    timeout    = 30

    def __init__(self) -> None:
        super().__init__()
        self._bg_tasks: set[asyncio.Task] = set()
        # Создаём llm_with_tools один раз — не bind_tools на каждый запрос
        self._llm_tools = self._llm.bind_tools([VAULT_TOOL])
        # Кэш статичной части системного промпта
        self._static_prompt_cache: str | None = None
        self._static_prompt_ts: float = 0.0

    def _system_prompt(self) -> str:
        return settings.system_prompt

    def _run_background(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    def _get_static_prompt(self) -> str:
        """Статичная часть промпта — кэшируем на TTL секунд."""
        now = time.monotonic()
        if self._static_prompt_cache and (now - self._static_prompt_ts) < _SYSTEM_CACHE_TTL:
            return self._static_prompt_cache
        prompt = settings.system_prompt + _HARD_RULES
        self._static_prompt_cache = prompt
        self._static_prompt_ts    = now
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
                return f"Что известно о Сократе:\n{facts}"
            return ""

        async def _get_vault_summary():
            try:
                from app.integrations.obsidian import get_tasks_summary, vault_available
                if vault_available():
                    summary = get_tasks_summary()
                    if summary != "задач нет":
                        return f"Текущие задачи Сократа: {summary}"
            except Exception:
                pass
            return ""

        # Запускаем всё параллельно
        (
            emot_ctx, conv_ctx, personality_ctx, state_ctx,
            interaction_ctx, observation_ctx, memory_ctx, vault_summary
        ) = await asyncio.gather(
            _get_moods(), _get_conv(), _get_personality(), _get_state(),
            _get_interaction(), _get_observation(), _get_memory(), _get_vault_summary()
        )

        # ── 3. Синхронные вычисления (быстрые) ───────────────────────────────
        opinion_ctx = get_opinion_hint(ctx.message)
        task_type, _, tone_hint = detect_task_type(ctx.message)
        length_hint = get_response_length_hint(ctx.message, is_voice=is_voice)

        # ── 4. Собираем системный промпт ─────────────────────────────────────
        system = self._get_static_prompt()

        for block in filter(None, [
            emot_ctx, conv_ctx, personality_ctx, state_ctx,
            opinion_ctx, interaction_ctx, observation_ctx, memory_ctx,
            vault_summary, tone_hint, length_hint,
        ]):
            system += f"\n\n{block}"

        # Динамические блоки из extra
        decisions = ctx.extra.get("decisions_block", "")
        if decisions:
            system += f"\n\n{decisions}"

        resume = ctx.extra.get("resume_bridge", "")
        if resume:
            system += (
                f"\n\nВАЖНО: Сократ вернулся после паузы. Начни ответ с естественного "
                f"упоминания того о чём говорили: '{resume}' — "
                f"вплети это органично, не как отдельный абзац."
            )

        system += build_reminder_prompt_block(now_utc)

        # ── 5. Сообщение ──────────────────────────────────────────────────────
        content = ctx.message
        if ctx.search_results:
            content = f"{ctx.message}\n\n[Контекст из поиска:]\n{ctx.search_results}"

        messages = [
            SystemMessage(content=system),
            *ctx.history,
            HumanMessage(content=content),
        ]

        # ── 6. Вызов модели с vault tool ──────────────────────────────────────
        response   = await self._llm_tools.ainvoke(messages)
        raw        = str(response.content) if response.content else ""
        tool_calls = getattr(response, "tool_calls", []) or []
        vault_results: list[str] = []

        if tool_calls:
            from langchain_core.messages import ToolMessage as _TM
            results = await process_tool_calls(tool_calls, ctx.user_id)
            vault_results = [r["result"] for r in results]
            tool_msgs = [
                _TM(content=r["result"], tool_call_id=r["tool_call_id"])
                for r in results
            ]
            final = await self._llm.ainvoke(messages + [response] + tool_msgs)
            raw   = str(final.content)
            for r in results:
                logger.info("🔧 vault: %s", r["result"][:80])

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
                "vault_ops": vault_results,
            },
        )

    async def _track_mood(self, ctx: AgentContext) -> None:
        try:
            mood = await detect_mood(ctx.message, self._llm)
            if mood != "нейтрально":
                save_mood(ctx.user_id, mood, ctx.message[:100])
        except Exception:
            logger.debug("mood tracking failed", exc_info=True)
