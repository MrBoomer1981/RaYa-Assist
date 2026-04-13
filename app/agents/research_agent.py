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
Ты RaYa — исследователь.

КРИТИЧНО:
- Если в контексте есть [Данные из поиска] — они ПРИОРИТЕТНЫ над твоими внутренними знаниями
- Если видишь дату получения данных — укажи её в ответе
- Если поиска нет — явно предупреди что данные могут быть устаревшими

Задача:
- Синтезируй информацию из нескольких источников
- Укажи что точно известно, что спорно, что неизвестно
- Для событий/миссий/проектов — всегда указывай актуальность
- Обращайся к пользователю по имени.\
"""

_SYSTEM_FACT = """\
Ты RaYa — верификатор фактов.

КРИТИЧНО: [Данные из поиска] — ПРИОРИТЕТНЫ над внутренними знаниями.

Задача:
- Аргументы ЗА (со ссылкой на источник из поиска если есть)
- Аргументы ПРОТИВ
- Вердикт: [Подтверждено] / [Вероятно] / [Спорно] / [Опровергнуто]
- Укажи насколько свежие данные
- Не делай выводов сверх данных
- Обращайся к пользователю по имени.\
"""

_SYSTEM_SCIENCE = """\
Ты RaYa — научный аналитик.

КРИТИЧНО: [Данные из поиска] — ПРИОРИТЕТНЫ над внутренними знаниями.

Правила:
- Разделяй факты и интерпретации
- Степень достоверности: [Подтверждено] / [Вероятно] / [Спорно] / [Опровергнуто]
- Опирайся на данные из поиска, указывай их дату
- Признавай когда данных недостаточно
- Обращайся к пользователю по имени.\
"""

_SYSTEMS = {
    "research":   _SYSTEM_RESEARCH,
    "fact_check": _SYSTEM_FACT,
    "science":    _SYSTEM_SCIENCE,
}


def _build_search_queries(message: str, mode: str) -> list[str]:
    """
    Строит несколько поисковых запросов для одной темы.
    - Разные углы = лучше покрытие
    - Для международных тем — добавляет английский вариант
    - Для науки — academic-формат
    """
    from datetime import datetime
    from app.search_service import _is_international, _translate_key_terms

    base = message.strip()
    year = datetime.utcnow().year
    queries = [base]

    if mode == "science":
        queries += [
            f"{base} research study {year}",
            f"{base} scientific evidence",
        ]
        if _is_international(base):
            en = _translate_key_terms(base)
            queries.append(f"{en} research {year}")
        return queries[:4]

    # research mode
    queries.append(f"{base} {year}")

    event_kw = ("миссия", "запуск", "артемид", "artemis", "spacex", "starship",
                 "mission", "launch", "program", "status", "полёт", "старт")
    is_event = any(kw in base.lower() for kw in event_kw)

    if is_event:
        queries.append(f"{base} latest update {year}")

    # Международная тема — добавляем английский вариант
    if _is_international(base):
        en = _translate_key_terms(base)
        if en.lower() != base.lower():
            queries.append(f"{en} {year}")
            if is_event:
                queries.append(f"{en} latest news {year}")

    return queries[:5]  # не более 5 запросов


class ResearchAgent(BaseAgent):
    agent_name = "research"
    timeout    = 45

    def _system_prompt(self) -> str:
        return _SYSTEM_RESEARCH

    async def _execute(self, ctx: AgentContext) -> AgentResult:
        mode   = _detect_mode(ctx.message)
        system = _SYSTEMS[mode]

        # ── Поиск: максимально агрессивный для research агента ────────────────
        search_block = ""
        if ctx.search_results:
            # Уже есть результаты от llm_service — используем, но дополняем
            search_block = f"\n\n[Данные из поиска]:\n{ctx.search_results[:3000]}"
        
        if settings.search_enabled and not search_block:
            try:
                from app.search_service import SearchService
                svc = SearchService()

                if mode == "fact_check":
                    # fact_check: оба направления (за и против)
                    raw = await svc.fact_check(ctx.message)
                    if raw:
                        search_block = f"\n\n[Данные из поиска]:\n{raw[:4000]}"
                else:
                    # research/science: полный smart_search pipeline
                    # Search → Evaluate → Refine → Full-page fetch
                    raw = await svc.smart_search(
                        ctx.message, mode=mode, max_results=5
                    )
                    if raw and "[Поиск не дал результатов]" not in raw:
                        search_block = f"\n\n[Данные из поиска]:\n{raw[:5000]}"

                    # Дополнительные углы через multi_search для research
                    if mode == "research":
                        extra_queries = _build_search_queries(ctx.message, mode)
                        extra_queries = [q for q in extra_queries[1:] if q != ctx.message]
                        if extra_queries:
                            extra = await svc.multi_search(extra_queries[:2], max_per_query=3)
                            if extra:
                                extra_text = svc._format_raw(extra, 400)
                                search_block += f"\n\n[Дополнительные источники]:\n{extra_text[:2000]}"
            except Exception:
                logger.debug("research: поиск недоступен", exc_info=True)
        
        # Если после всего search_block пустой — явно скажем LLM что нет данных
        if not search_block:
            search_block = "\n\n[Поиск недоступен или не дал результатов. Отвечай на основе знаний, явно указав что данные могут быть устаревшими.]"

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
        logger.info("🔬 ResearchAgent: режим '%s' | user_id=%s | поиск: %s",
                    mode, ctx.user_id, "да" if "[Данные из поиска]" in search_block else "нет")

        return AgentResult(
            success=True,
            content=str(resp.content),
            agent_name=self.agent_name,
            needs_critic=True,
            metadata={"mode": mode},
        )


