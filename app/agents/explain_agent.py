"""
explain_agent.py — агент объяснений, структурирования и декомпозиции.

Три режима (определяются автоматически по запросу):
  explain    — объяснить сложную вещь понятно (с аналогиями, уровнями)
  structure  — структурировать информацию / мысли
  breakdown  — разбить задачу/процесс на шаги

Отличие от planning_agent:
  planning   — стратегическое планирование с дедлайнами, рисками, метриками
  explain    — понять концепцию, разобраться в теме, привести к структуре
"""
import logging
import re

from langchain_core.messages import SystemMessage, HumanMessage

from app.agents.base_agent import AgentContext, AgentResult, BaseAgent

logger = logging.getLogger(__name__)

# ── Системные промпты по режимам ──────────────────────────────────────────────

_SYSTEM_EXPLAIN = """\
Ты RaYa — объясняешь сложные вещи так, чтобы они щёлкнули в голове.

Принципы объяснения:
1. Начинай с сути в одном предложении — без предисловий
2. Используй аналогию из реальной жизни — одну, точную
3. Разбери механизм: почему это работает именно так
4. Покажи где это встречается на практике
5. Если тема многоуровневая — спроси какой уровень нужен

Форматы в зависимости от запроса:
- «Объясни как ребёнку» → простая аналогия, никаких терминов
- «Объясни подробно» → полное разворачивание с примерами
- По умолчанию → средний уровень: суть + аналогия + практика

Никогда:
- Не начинай с «Конечно!» или «Отличный вопрос!»
- Не перечисляй определения без объяснения смысла
- Не используй жаргон без расшифровки

Обращайся только «Сократ»."""

_SYSTEM_STRUCTURE = """\
Ты RaYa — структурируешь хаос в понятную форму.

Что умеешь:
- Взять набор мыслей и выстроить их логически
- Найти главное и отделить от второстепенного
- Выявить противоречия и пробелы
- Предложить оптимальную структуру для конкретной цели

Как работаешь:
1. Сначала определи цель структуры — для чего она нужна?
2. Выдели ключевые блоки (3-7 штук — больше не воспринимается)
3. Установи связи между блоками
4. Выдели что важно, что можно убрать

Форматы вывода:
- Иерархия с отступами если есть вложенность
- Таблица если нужно сравнение
- Линейный список если это последовательность
- Дерево решений если есть условия

Обращайся только «Сократ»."""

_SYSTEM_BREAKDOWN = """\
Ты RaYa — разбиваешь сложное на выполнимые шаги.

Принципы хорошего breakdown:
1. Каждый шаг — одно конкретное действие (глагол + объект)
2. Шаг выполним за один сеанс работы
3. Виден результат каждого шага
4. Порядок шагов логически обоснован

Что добавляешь по необходимости:
- Зависимости: «Шаг 3 требует результата шага 1»
- Развилки: «Если X — делаем A, если Y — делаем B»
- Предупреждения: где обычно застревают
- Быстрые победы: что сделать первым для видимого прогресса

Чего НЕ делаешь:
- Не добавляешь лишние шаги «для полноты»
- Не пишешь очевидное — только то что реально нужно знать
- Не делаешь шаги слишком мелкими (не «открой файл»)

Обращайся только «Сократ»."""

# ── Определение режима ────────────────────────────────────────────────────────

_EXPLAIN_KEYWORDS = {
    "объясни", "объяснение", "что такое", "как работает", "почему",
    "как понять", "не понимаю", "растолкуй", "расскажи что", "eli5",
    "простыми словами", "как ребёнку", "в чём смысл", "что значит",
}

_STRUCTURE_KEYWORDS = {
    "структурируй", "структура", "упорядочи", "организуй",
    "приведи в порядок", "систематизируй", "классифицируй",
    "выдели главное", "что важно", "расставь приоритеты",
    "оформи", "сгруппируй", "категории",
}

_BREAKDOWN_KEYWORDS = {
    "разбей", "по шагам", "пошагово", "с чего начать", "как сделать",
    "инструкция", "алгоритм", "процесс", "как реализовать",
    "разложи", "breakdown", "как внедрить", "как запустить",
}


def _detect_mode(message: str) -> str:
    msg = message.lower()
    explain_score  = sum(1 for kw in _EXPLAIN_KEYWORDS   if kw in msg)
    structure_score= sum(1 for kw in _STRUCTURE_KEYWORDS if kw in msg)
    breakdown_score= sum(1 for kw in _BREAKDOWN_KEYWORDS if kw in msg)

    scores = {
        "explain":   explain_score,
        "structure": structure_score,
        "breakdown": breakdown_score,
    }
    # Если нет явного сигнала — explain по умолчанию
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else "explain"


def _system_for_mode(mode: str) -> str:
    return {
        "explain":   _SYSTEM_EXPLAIN,
        "structure": _SYSTEM_STRUCTURE,
        "breakdown": _SYSTEM_BREAKDOWN,
    }[mode]


def _enrich_prompt(message: str, mode: str, ctx: AgentContext) -> str:
    """Обогащает запрос контекстом для лучшего результата."""
    extras = []

    # Добавляем факты о пользователе если есть
    if ctx.memory_facts:
        facts = "\n".join(f"- {f}" for f in ctx.memory_facts[:3])
        extras.append(f"Что знаешь о Сократе:\n{facts}")

    # Добавляем результаты поиска если есть
    if ctx.search_results:
        extras.append(f"Актуальная информация:\n{ctx.search_results[:800]}")

    if not extras:
        return message

    context_block = "\n\n".join(extras)
    return f"{message}\n\n---\nКонтекст:\n{context_block}"


class ExplainAgent(BaseAgent):
    agent_name = "explain"
    timeout    = 50

    def _system_prompt(self) -> str:
        return _SYSTEM_EXPLAIN  # overridden per-request

    async def _execute(self, ctx: AgentContext) -> AgentResult:
        mode   = _detect_mode(ctx.message)
        system = _system_for_mode(mode)
        prompt = _enrich_prompt(ctx.message, mode, ctx)

        logger.info("📚 ExplainAgent: режим '%s' | user_id=%s", mode, ctx.user_id)

        # История разговора для контекста
        history_msgs = []
        for msg in ctx.history[-6:]:  # последние 6 сообщений
            role = msg.__class__.__name__
            if role == "HumanMessage":
                history_msgs.append(HumanMessage(content=msg.content))
            elif role == "AIMessage":
                from langchain_core.messages import AIMessage
                history_msgs.append(AIMessage(content=msg.content))

        messages = (
            [SystemMessage(content=system)]
            + history_msgs
            + [HumanMessage(content=prompt)]
        )

        response = await self._llm.ainvoke(messages)
        reply    = str(response.content).strip()

        return AgentResult(
            success    = True,
            content    = reply,
            agent_name = self.agent_name,
            needs_critic = True,   # критик проверяет точность объяснений
            metadata   = {"mode": mode},
        )
