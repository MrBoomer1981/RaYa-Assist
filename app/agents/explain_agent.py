"""
explain_agent.py — объяснения, структурирование, планирование.

Объединяет explain_agent + planning_agent.

Режимы:
  explain   — объяснить сложную концепцию понятным языком
  structure — структурировать информацию / мысли
  breakdown — разбить на пошаговую инструкцию
  plan      — план с дедлайнами, рисками, метриками
"""
import logging
import re

from langchain_core.messages import HumanMessage, SystemMessage

from app.agents.base_agent import AgentContext, AgentResult, BaseAgent, strip_history

logger = logging.getLogger(__name__)

_EXPLAIN_KW   = ("объясни", "что такое", "как работает", "почему", "не понимаю",
                 "простыми словами", "как ребёнку", "объяснение")
_STRUCTURE_KW = ("структурируй", "упорядочи", "приведи в порядок", "выдели главное",
                 "разложи", "систематизируй")
_BREAKDOWN_KW = ("пошагово", "инструкция", "как сделать", "разбей на шаги")
_PLAN_KW      = ("составь план", "распланируй", "декомпозиция", "roadmap",
                 "дорожная карта", "план на", "план по", "как реализовать",
                 "оцени риски", "тайм-менеджмент", "шаги для")


def _detect_mode(message: str) -> str:
    m = message.lower()
    if any(kw in m for kw in _PLAN_KW):      return "plan"
    if any(kw in m for kw in _BREAKDOWN_KW): return "breakdown"
    if any(kw in m for kw in _STRUCTURE_KW): return "structure"
    return "explain"


_SYSTEM_EXPLAIN = """\
Ты RaYa — объясняешь сложные вещи понятно.

Подход:
- Начни с сути в одном предложении
- Используй аналогии из обычной жизни
- Разбей на уровни: основное → детали → нюансы
- Приведи конкретный пример
- Обращайся к пользователю по имени.\
"""

_SYSTEM_STRUCTURE = """\
Ты RaYa — структурируешь информацию и мысли.

Подход:
- Выяви ключевые элементы
- Сгруппируй по смыслу
- Расставь приоритеты
- Покажи связи между элементами
- Обращайся к пользователю по имени.\
"""

_SYSTEM_BREAKDOWN = """\
Ты RaYa — разбиваешь задачи на конкретные шаги.

Подход:
- Каждый шаг = одно действие с чётким результатом
- Укажи что нужно для каждого шага
- Добавь временные оценки если уместно
- Отметь зависимости между шагами
- Обращайся к пользователю по имени.\
"""

_SYSTEM_PLAN = """\
Ты RaYa — эксперт по планированию.

Что делаешь:
📋 ДЕКОМПОЗИЦИЯ — разбиваешь на конкретные шаги (каждый выполним за 1-4ч)
📅 ДЕДЛАЙНЫ — предлагаешь реалистичные сроки
⚠️ РИСКИ — указываешь что может пойти не так
📊 МЕТРИКИ — как понять что план выполнен

Если задач несколько — предлагаешь сохранить в список.
Обращайся к пользователю по имени.\
"""

_SYSTEMS = {
    "explain":   _SYSTEM_EXPLAIN,
    "structure": _SYSTEM_STRUCTURE,
    "breakdown": _SYSTEM_BREAKDOWN,
    "plan":      _SYSTEM_PLAN,
}


class ExplainAgent(BaseAgent):
    agent_name = "explain"
    timeout    = 35

    def _system_prompt(self) -> str:
        return _SYSTEM_EXPLAIN

    async def _execute(self, ctx: AgentContext) -> AgentResult:
        mode   = _detect_mode(ctx.message)
        system = _SYSTEMS[mode]

        facts_block = ""
        if ctx.memory_facts:
            facts_block = "\n\n[Контекст]: " + "; ".join(ctx.memory_facts[:3])

        history_msgs = strip_history(ctx.history, limit=6)
        messages     = [
            SystemMessage(content=system),
            *history_msgs,
            HumanMessage(content=ctx.message + facts_block),
        ]

        resp  = await self._llm.ainvoke(messages)
        reply = str(resp.content)

        logger.info("💡 ExplainAgent: режим '%s' | user_id=%s", mode, ctx.user_id)

        # Если план — сохраняем шаги как задачи в БД
        metadata = {"mode": mode}
        if mode == "plan":
            tasks = re.findall(r"^\s*[-•\d+\.]\s*(.+)$", reply, re.MULTILINE)
            if tasks and len(tasks) >= 2:
                try:
                    from app.database import save_task
                    for t in tasks[:10]:
                        save_task(ctx.user_id, t.strip(), 2, "")
                    metadata["tasks_added"] = len(tasks[:10])
                except Exception:
                    logger.debug("explain: не удалось сохранить план в БД")

        return AgentResult(
            success=True,
            content=reply,
            agent_name=self.agent_name,
            needs_critic=(mode in ("explain", "plan")),
            metadata=metadata,
        )
