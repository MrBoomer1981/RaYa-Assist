"""
diary_agent.py — агент личного дневника.
Хранение, рефлексия, анализ паттернов. Данные приватны.
"""
import logging

from app.agents.base_agent import AgentContext, AgentResult, BaseAgent
from app.database import load_diary_entries, save_diary_entry

logger = logging.getLogger(__name__)

_SYSTEM = """\
Ты хранитель личного дневника Сократа.

Твои задачи:
- Принимать и сохранять личные записи
- Помогать с рефлексией и осмыслением событий
- Замечать паттерны в настроении и мыслях
- Задавать глубокие вопросы которые помогают думать
- Поддерживать без лишних советов — если Сократ хочет выговориться

Тон: тёплый, внимательный, без осуждения.
Обращайся только "Сократ". Никогда не делись записями с другими агентами."""

_QUESTION_KEYWORDS = ("покажи", "прочитай", "что я писал", "найди", "когда я")


class DiaryAgent(BaseAgent):
    agent_name = "diary"
    timeout = 30

    def _system_prompt(self) -> str:
        return _SYSTEM

    async def _execute(self, ctx: AgentContext) -> AgentResult:
        # Добавляем последние записи как контекст для модели
        recent = load_diary_entries(ctx.user_id, limit=5)
        context_str = ""
        if recent:
            lines = [f"[{dt}]: {entry[:200]}" for dt, entry in recent]
            context_str = "\n\nПоследние записи:\n" + "\n".join(lines)

        messages = self._build_messages(ctx, user_content=ctx.message + context_str)
        response  = await self._llm.ainvoke(messages)
        reply     = str(response.content)

        # Сохраняем только новые записи — не вопросы о дневнике
        entry_id = 0
        msg_lower = ctx.message.lower()
        is_question = any(kw in msg_lower for kw in _QUESTION_KEYWORDS)
        if not is_question:
            entry_id = save_diary_entry(ctx.user_id, ctx.message)
            logger.info("📔 Запись #%d сохранена | user_id=%s", entry_id, ctx.user_id)

        return AgentResult(
            success=True,
            content=reply,
            agent_name=self.agent_name,
            needs_critic=False,
            metadata={"entry_saved": entry_id > 0, "entry_id": entry_id},
        )
