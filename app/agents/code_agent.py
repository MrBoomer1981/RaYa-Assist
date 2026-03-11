"""
code_agent.py — агент для работы с кодом.

Умеет:
- Писать код с нуля
- Отлаживать и исправлять баги
- Делать code review с конкретными замечаниями
- Объяснять алгоритмы и паттерны
- Рефакторить (читаемость, производительность, архитектура)
- Оценивать сложность и предлагать оптимизации
- Писать тесты
"""
import logging
import re

from app.agents.base_agent import AgentContext, AgentResult, BaseAgent

logger = logging.getLogger(__name__)

_SYSTEM = """\
Ты RaYa — senior-разработчик и ментор по коду.

Режимы работы:

✍️ НАПИСАТЬ КОД
- Чистый, читаемый, с комментариями в сложных местах
- Обрабатываешь edge cases
- Добавляешь типизацию где уместно
- Объясняешь ключевые решения кратко

🐛 ОТЛАДКА
- Сначала называешь причину бага — одним предложением
- Показываешь исправленный код
- Объясняешь почему это работает

🔍 CODE REVIEW
Структура ревью:
1. Общая оценка (1-2 предложения)
2. Критичные проблемы 🔴 (баги, безопасность, утечки)
3. Улучшения 🟡 (читаемость, производительность)
4. Хорошие решения ✅ (что сделано правильно)
5. Итоговый балл: X/10

🧠 ОБЪЯСНЕНИЕ АЛГОРИТМА
- Идея простыми словами (аналогия если помогает)
- Псевдокод или реальный пример
- Сложность O(n) — время и память
- Когда использовать, когда не использовать

♻️ РЕФАКТОРИНГ
- Показываешь: было → стало
- Объясняешь каждое изменение (1-2 слова)
- Не переусложняешь — простой код лучше умного

🧪 ТЕСТЫ
- Unit-тесты с edge cases
- Понятные названия: test_should_return_X_when_Y
- Минимум моков, максимум реальной логики

Язык угадываешь из контекста. Если непонятно — спрашиваешь.
Обращайся только "Сократ".

## Диаграммы (Mermaid)
Когда полезнее показать структуру — используй:
```mermaid
graph TD / flowchart / sequenceDiagram / classDiagram
```
Применяй для архитектуры, потоков данных, схем БД.
"""

_MODE_KEYWORDS = {
    "review":    ("review", "ревью", "проверь код", "оцени код", "посмотри код"),
    "debug":     ("баг", "ошибка", "не работает", "падает", "debug", "traceback", "exception"),
    "explain":   ("объясни", "как работает", "что такое", "расскажи про алгоритм", "паттерн"),
    "refactor":  ("рефактор", "улучши код", "оптимизируй", "перепиши", "почисти"),
    "test":      ("напиши тест", "unit test", "тесты для", "покрой тестами"),
    "write":     ("напиши", "реализуй", "сделай функцию", "создай класс"),
}

_LANGUAGES = {
    "python":     ("python", "питон", "py", "django", "flask", "fastapi", "asyncio"),
    "javascript": ("javascript", "js", "node", "react", "vue", "typescript", "ts"),
    "sql":        ("sql", "запрос", "select", "insert", "update", "база данных"),
    "bash":       ("bash", "shell", "скрипт", "терминал", "linux", "chmod"),
    "go":         ("golang", "go ", " go\n"),
    "rust":       ("rust", "cargo"),
}


def _detect_mode(message: str) -> str:
    msg = message.lower()
    for mode, keywords in _MODE_KEYWORDS.items():
        if any(kw in msg for kw in keywords):
            return mode
    return "write"


def _detect_language(message: str) -> str:
    msg = message.lower()
    for lang, keywords in _LANGUAGES.items():
        if any(kw in msg for kw in keywords):
            return lang
    return "unknown"


class CodeAgent(BaseAgent):
    agent_name = "code"
    timeout    = 50  # код и ревью требуют времени

    def _system_prompt(self) -> str:
        return _SYSTEM

    async def _execute(self, ctx: AgentContext) -> AgentResult:
        mode     = _detect_mode(ctx.message)
        language = _detect_language(ctx.message)

        messages = self._build_messages(ctx)
        response = await self._llm.ainvoke(messages)
        content  = str(response.content)

        # Code review и объяснения — всегда через критика
        needs_critic = mode in ("review", "explain")

        return AgentResult(
            success=True,
            content=content,
            agent_name=self.agent_name,
            needs_critic=needs_critic,
            metadata={"mode": mode, "language": language},
        )
