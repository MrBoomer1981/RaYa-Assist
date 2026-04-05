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

        await _auto_save_zettel(self._llm, ctx.message, str(resp.content), bool(search_block))


        return AgentResult(
            success=True,
            content=str(resp.content),
            agent_name=self.agent_name,
            needs_critic=True,
            metadata={"mode": mode},
        )


# ── Автосохранение результатов поиска в Zettelkasten ─────────────────────────

async def _auto_save_zettel(llm, query: str, result: str, has_search: bool) -> None:
    """Сохраняет найденное в Zettelkasten. Дополняет существующее если похоже."""
    if not has_search:
        return
    try:
        import json as _j
        from app.integrations.obsidian import (
            add_zettel, list_zettel_titles, update_zettel, vault_available,
        )
        from app.utils import strip_json
        from langchain_core.messages import HumanMessage as _HM

        if not vault_available():
            return

        titles       = list_zettel_titles()
        existing_str = "\n".join(f"- {e['id']}: {e['title']}" for e in titles[-15:]) or "нет"

        dedup_q = (
            "Информация по запросу: " + query[:200] + "\n\n"
            "Существующие карточки:\n" + existing_str + "\n\n"
            "Это новая тема или дополнение к существующей карточке?\n"
            'JSON: {"decision":"new|update","existing_id":"ID или пусто"}'
        )
        dr    = await llm.ainvoke([_HM(content=dedup_q)])
        dedup = _j.loads(strip_json(str(dr.content)))

        if dedup.get("decision") == "update" and dedup.get("existing_id"):
            update_zettel(dedup["existing_id"], result[:600])
        else:
            zettel_q = (
                "Создай атомарную Zettelkasten карточку.\n"
                "Запрос: " + query[:200] + "\n"
                "Найденное: " + result[:500] + "\n\n"
                'JSON: {"title":"название 5-8 слов","content":"суть 2-4 предл","tags":["тег1","тег2"]}'
            )
            zr = await llm.ainvoke([_HM(content=zettel_q)])
            zd = _j.loads(strip_json(str(zr.content)))
            add_zettel(zd.get("title", query[:50]), zd.get("content", result[:400]),
                       zd.get("tags", []))
    except Exception:
        import logging
        logging.getLogger(__name__).debug("_auto_save_zettel failed", exc_info=True)
