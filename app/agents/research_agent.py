"""
research_agent.py — агент глубокого исследования.

Три режима (определяются автоматически):
  research   — исследование темы с нескольких источников
  fact_check — проверка конкретного утверждения
  synthesis  — объединение знаний из разных областей

Отличие от science_agent:
  science   — верификация конкретных фактов, один поиск
  research  — итеративное исследование темы, множество запросов,
              синтез противоречий, выводы с уровнями достоверности
"""
import logging
import re

from langchain_core.messages import SystemMessage, HumanMessage

from app.agents.base_agent import AgentContext, AgentResult, BaseAgent
from app.config import settings

logger = logging.getLogger(__name__)

# ── Ключевые слова для режимов ────────────────────────────────────────────────

_RESEARCH_KW  = {
    "исследуй", "изучи", "расскажи подробно", "найди информацию",
    "что известно о", "углублённо", "исследование", "обзор темы",
    "проанализируй тему", "что говорят о", "последние данные",
}
_FACTCHECK_KW = {
    "правда ли", "это правда", "верно ли", "проверь факт",
    "так ли это", "миф или", "на самом деле", "действительно ли",
    "проверь", "подтверди", "опровергни",
}
_SYNTHESIS_KW = {
    "объедини", "сравни", "сопоставь", "с разных сторон",
    "разные мнения", "разные подходы", "pros and cons",
    "за и против", "плюсы и минусы разных",
}


def _detect_mode(message: str) -> str:
    msg = message.lower()
    scores = {
        "research":   sum(1 for kw in _RESEARCH_KW   if kw in msg),
        "fact_check": sum(1 for kw in _FACTCHECK_KW  if kw in msg),
        "synthesis":  sum(1 for kw in _SYNTHESIS_KW  if kw in msg),
    }
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else "research"


# ── Системные промпты ─────────────────────────────────────────────────────────

_SYSTEM_BASE = """\
Ты RaYa — исследовательский агент. Работаешь с реальными данными из поиска.

Принципы:
- Разделяй факты, интерпретации и мнения
- Фиксируй противоречия между источниками явно
- Указывай степень достоверности: ✅ Подтверждено / ⚠️ Спорно / ❓ Неясно
- Признавай пробелы: "данных по этому пункту недостаточно"
- Дата важна: отмечай когда информация может устареть

Обращайся только «Сократ»."""

_SYSTEM_RESEARCH = _SYSTEM_BASE + """

## Режим: Исследование темы

Структура ответа:
1. **Ключевые факты** — что точно известно
2. **Спорные моменты** — где источники расходятся
3. **Пробелы** — чего мы не знаем
4. **Вывод** — твоя синтезированная позиция

Не пересказывай источники по очереди — синтезируй."""

_SYSTEM_FACTCHECK = _SYSTEM_BASE + """

## Режим: Проверка факта

Структура ответа:
1. **Утверждение** — что именно проверяем
2. **Доказательства ЗА** — что подтверждает
3. **Доказательства ПРОТИВ** — что опровергает или усложняет
4. **Вердикт** — ✅ / ⚠️ / ❓ с объяснением

Честно если данных недостаточно для однозначного вывода."""

_SYSTEM_SYNTHESIS = _SYSTEM_BASE + """

## Режим: Синтез из нескольких источников

Структура ответа:
1. **Разные подходы/мнения** — что говорят разные стороны
2. **Точки пересечения** — где все согласны
3. **Принципиальные разногласия** — где расходятся и почему
4. **Сбалансированный вывод** — без навязывания одной позиции

Покажи карту мнений, не выбирай сторону без оснований."""


# ── Генератор поисковых запросов ──────────────────────────────────────────────

async def _generate_queries(message: str, mode: str, llm) -> list[str]:
    """
    Просит LLM сгенерировать 2-4 поисковых запроса для покрытия темы.
    Разные углы: основной факт, контекст, критика, актуальность.
    """
    mode_instruction = {
        "research":   "разные аспекты темы: основное, история, текущее состояние, критика",
        "fact_check": "подтверждение утверждения И его опровержение/усложнение",
        "synthesis":  "разные точки зрения и подходы к теме",
    }.get(mode, "разные аспекты")

    prompt = (
        f"Запрос: {message}\n\n"
        f"Сгенерируй 3 поисковых запроса на русском для покрытия темы ({mode_instruction}).\n"
        f"Запросы должны быть короткими (3-6 слов) и охватывать разные углы.\n"
        f"Верни ТОЛЬКО список через новую строку, без нумерации и пояснений."
    )
    try:
        response = await llm.ainvoke([HumanMessage(content=prompt)])
        raw = str(response.content).strip()
        queries = [
            q.strip().lstrip("•-123456789. ")
            for q in raw.split("\n")
            if q.strip() and len(q.strip()) > 3
        ]
        # Всегда включаем оригинальный запрос
        if message not in queries:
            queries.insert(0, message[:100])
        return queries[:4]
    except Exception:
        return [message[:100]]


class ResearchAgent(BaseAgent):
    agent_name = "research"
    timeout    = 90  # исследование требует времени

    def __init__(self) -> None:
        super().__init__()
        self._search = None
        if settings.search_enabled:
            from app.search_service import SearchService
            self._search = SearchService()
            logger.info("🔍 ResearchAgent: поиск включён")

    def _system_prompt(self) -> str:
        return _SYSTEM_RESEARCH  # overridden per-request

    async def _execute(self, ctx: AgentContext) -> AgentResult:
        mode = _detect_mode(ctx.message)
        logger.info("🔍 ResearchAgent: режим '%s' | user_id=%s", mode, ctx.user_id)

        # Выбираем системный промпт
        system = {
            "research":   _SYSTEM_RESEARCH,
            "fact_check": _SYSTEM_FACTCHECK,
            "synthesis":  _SYSTEM_SYNTHESIS,
        }.get(mode, _SYSTEM_RESEARCH)

        # ── Сбор данных ───────────────────────────────────────────────────────
        search_results = ""
        sources_count  = 0

        if self._search:
            try:
                if mode == "fact_check":
                    # Специальный поиск с двух сторон
                    raw = await self._search.fact_check(ctx.message)
                    search_results = raw
                    sources_count  = raw.count("Источник:") + raw.count("[")
                else:
                    # Генерируем несколько запросов
                    queries = await _generate_queries(
                        ctx.message, mode, self._llm
                    )
                    logger.info(
                        "🔍 Research: запросы %s | user_id=%s",
                        queries, ctx.user_id,
                    )
                    results = await self._search.multi_search(queries, max_per_query=3)
                    sources_count = len(results)

                    # Форматируем для промпта
                    if results:
                        parts = []
                        for r in results[:8]:
                            chunk = []
                            if r.get("title"):   chunk.append(f"[{r['title']}]")
                            if r.get("content"): chunk.append(r["content"])
                            if r.get("url"):     chunk.append(f"Источник: {r['url']}")
                            if chunk:
                                parts.append("\n".join(chunk))
                        search_results = "\n\n---\n\n".join(parts)

            except Exception:
                logger.exception("ResearchAgent: ошибка поиска")

        # ── Формируем промпт ──────────────────────────────────────────────────
        content_parts = [ctx.message]

        if ctx.memory_facts:
            facts = "\n".join(f"- {f}" for f in ctx.memory_facts[:3])
            content_parts.append(f"\nЧто знаю о Сократе:\n{facts}")

        if search_results:
            content_parts.append(
                f"\n\n=== ДАННЫЕ ИЗ ПОИСКА ({sources_count} источников) ===\n"
                + search_results
                + "\n=== КОНЕЦ ДАННЫХ ==="
            )
        else:
            content_parts.append(
                "\n\nПоиск недоступен или не дал результатов. "
                "Используй только свои знания — честно укажи это."
            )

        # История разговора
        history_msgs = []
        for msg in ctx.history[-4:]:
            role = msg.__class__.__name__
            if role == "HumanMessage":
                history_msgs.append(HumanMessage(content=msg.content))
            elif role == "AIMessage":
                from langchain_core.messages import AIMessage
                history_msgs.append(AIMessage(content=msg.content))

        messages = (
            [SystemMessage(content=system)]
            + history_msgs
            + [HumanMessage(content="\n".join(content_parts))]
        )

        response = await self._llm.ainvoke(messages)
        reply    = str(response.content).strip()

        # Добавляем метку если поиск не использовался
        if not search_results and not ctx.search_results:
            reply += "\n\n_⚠️ Поиск недоступен — ответ на основе внутренних знаний._"

        return AgentResult(
            success    = True,
            content    = reply,
            agent_name = self.agent_name,
            needs_critic = True,
            metadata   = {
                "mode":          mode,
                "sources_count": sources_count,
                "search_used":   bool(search_results),
            },
        )
