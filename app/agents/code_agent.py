"""
code_agent.py — агент для работы с кодом.
Пишет, отлаживает, объясняет, делает code review.
"""
import logging

from app.agents.base_agent import AgentContext, AgentResult, BaseAgent

logger = logging.getLogger(__name__)

_SYSTEM = """\
Ты эксперт-программист в команде RaYa.
Специализация: написание кода, отладка, архитектурные решения, code review.

Правила:
- Пишешь чистый, читаемый код с комментариями
- Объясняешь решения кратко — почему так, а не иначе
- Указываешь на потенциальные проблемы и edge cases
- Предлагаешь улучшения если видишь их
- Используешь современные практики для каждого языка
- Если задача неоднозначна — уточняешь перед написанием

Обращайся к пользователю только "Сократ"."""


class CodeAgent(BaseAgent):
    agent_name = "code"
    timeout = 45  # код может требовать больше времени

    def _system_prompt(self) -> str:
        return _SYSTEM

    async def _execute(self, ctx: AgentContext) -> AgentResult:
        messages = self._build_messages(ctx)
        response = await self._llm.ainvoke(messages)
        content = str(response.content)

        return AgentResult(
            success=True,
            content=content,
            agent_name=self.agent_name,
            needs_critic=True,  # код всегда проверяем критиком
            metadata={"language": _detect_language(ctx.message)},
        )


def _detect_language(message: str) -> str:
    """Определяет язык программирования из сообщения."""
    msg = message.lower()
    languages = {
        "python": ["python", "питон", "py", "django", "flask", "fastapi"],
        "javascript": ["javascript", "js", "node", "react", "vue", "typescript"],
        "sql": ["sql", "запрос", "база данных", "select", "insert"],
        "bash": ["bash", "shell", "скрипт", "терминал", "linux"],
    }
    for lang, keywords in languages.items():
        if any(kw in msg for kw in keywords):
            return lang
    return "unknown"
