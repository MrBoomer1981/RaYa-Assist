"""
research_agent.py — исследование, проверка фактов, научный анализ.

Объединяет research_agent + science_agent.

Режимы (определяются автоматически):
  research   — глубокое исследование темы, синтез из нескольких источников
  fact_check — проверка конкретного утверждения (за/против + достоверность)
  science    — верификация научных данных, источники, степень достоверности
"""
import logging

from langchain_core.messages import HumanMessage, SystemMessage

from app.agents.base_agent import AgentContext, AgentResult, BaseAgent, strip_history
from app.config import settings

logger = logging.getLogger(__name__)

_RESEARCH_KW   = ("исследуй", "изучи", "найди информацию", "что известно", "обзор темы",
                  "расскажи подробно", "углублённо", "сравни подходы")
_FACT_KW       = ("это правда", "правда ли", "верно ли", "проверь факт", "миф или",
                  "на самом деле", "действительно ли", "за и против", "плюсы и минусы")
_SCIENCE_KW    = ("научн", "исследовани", "докажи", "источник", "достоверн",
                  "статистик", "данные говорят", "согласно исследованию")


def _detect_mode(message: str) -> str:
    m = message.lower()
    if any(kw in m for kw in _FACT_KW):    return "fact_check"
    if any(kw in m for kw in _SCIENCE_KW): return "science"
    return "research"


_SYSTEM_RESEARCH = """\
Ты RaYa — исследователь в команде ИИ-ассистента.

Задача: глубокое исследование темы из нескольких углов.
- Собери ключевые факты и точки зрения
- Укажи противоречия и спорные моменты
- Дай синтез: что точно известно, что под вопросом
- Используй данные из поиска если они есть
- Обращайся к пользователю по имени.\
"""

_SYSTEM_FACT = """\
Ты RaYa — верификатор фактов.

Задача: честная проверка утверждения.
- Аргументы ЗА (с источниками если есть)
- Аргументы ПРОТИВ
- Вердикт: [Подтверждено] / [Вероятно] / [Спорно] / [Опровергнуто]
- Не делай выводов сверх данных
- Обращайся к пользователю по имени.\
"""

_SYSTEM_SCIENCE = """\
Ты RaYa — научный аналитик.

Правила:
- Разделяй факты и интерпретации
- Степень достоверности: [Подтверждено] / [Вероятно] / [Спорно] / [Опровергнуто]
- Ссылайся на источники из контекста поиска
- Признавай когда данных недостаточно
- Указывай дату данных если может устареть
- Обращайся к пользователю по имени.\
"""

_SYSTEMS = {
    "research":   _SYSTEM_RESEARCH,
    "fact_check": _SYSTEM_FACT,
    "science":    _SYSTEM_SCIENCE,
}


class ResearchAgent(BaseAgent):
    agent_name = "research"
    timeout    = 45

    def _system_prompt(self) -> str:
        return _SYSTEM_RESEARCH

    async def _execute(self, ctx: AgentContext) -> AgentResult:
        mode   = _detect_mode(ctx.message)
        system = _SYSTEMS[mode]

        # Поиск если доступен
        search_block = ""
        if ctx.search_results:
            search_block = f"\n\n[Данные из поиска]:\n{ctx.search_results[:2000]}"
        elif settings.search_enabled:
            try:
                from app.search_service import SearchService
                svc     = SearchService()
                results = await svc.search(ctx.message)
                if results:
                    search_block = f"\n\n[Данные из поиска]:\n{results[:2000]}"
            except Exception:
                logger.debug("research: поиск недоступен", exc_info=True)

        history_msgs = strip_history(ctx.history, limit=4)
        facts_block  = ""
        if ctx.memory_facts:
            facts_block = "\n".join(f"- {f}" for f in ctx.memory_facts[:3])
            facts_block = f"\n\n[Контекст о пользователе]:\n{facts_block}"

        messages = [
            SystemMessage(content=system),
            *history_msgs,
            HumanMessage(content=ctx.message + search_block + facts_block),
        ]

        resp = await self._llm.ainvoke(messages)
        logger.info("🔬 ResearchAgent: режим '%s' | user_id=%s", mode, ctx.user_id)



        return AgentResult(
            success=True,
            content=str(resp.content),
            agent_name=self.agent_name,
            needs_critic=True,
            metadata={"mode": mode},
        )


