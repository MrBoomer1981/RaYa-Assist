"""
text_agent.py — агент анализа и работы с текстом.

Умеет:
- Резюмировать длинные тексты
- Редактировать и улучшать стиль
- Менять тон (формальный, дружелюбный, деловой)
- Переводить на другие языки
- Анализировать структуру и аргументацию
- Находить ошибки и неточности
- Писать тексты по шаблону (письма, посты, описания)
"""
import logging
import re

from app.agents.base_agent import AgentContext, AgentResult, BaseAgent

logger = logging.getLogger(__name__)

_SYSTEM = """\
Ты RaYa — эксперт по работе с текстом.

Что умеешь:
- Резюмировать: вычленяешь главное, убираешь воду
- Редактировать: улучшаешь стиль, убираешь повторы, делаешь текст живее
- Менять тон: формальный ↔ дружелюбный ↔ деловой ↔ экспертный
- Анализировать: структура, аргументы, слабые места, логика
- Переводить: точно и естественно, без буквализма
- Писать с нуля: посты, письма, описания, резюме, питчи

Как работаешь:
- Сначала понимаешь задачу — что именно нужно сделать с текстом
- Если задача неоднозначна — уточняешь один вопрос
- Объясняешь что изменила и почему (кратко, в конце)
- Предлагаешь альтернативный вариант если видишь очевидно лучший подход

Обращайся к пользователю по имени. Тон рабочий, без лишних слов."""

# Режимы работы — определяются по ключевым словам
_MODES = {
    "summarize": ("резюм", "кратко", "сжать", "тл;др", "tldr", "главное из"),
    "edit":      ("отредактируй", "улучши", "перепиши", "исправь стиль", "причеши"),
    "tone":      ("сделай формальн", "сделай дружелюбн", "измени тон", "деловой стиль"),
    "analyze":   ("проанализируй текст", "разбери", "найди ошибки", "оцени"),
    "translate": ("переведи", "перевод", "translate"),
    "write":     ("напиши письмо", "напиши пост", "напиши описание", "составь"),
}


def _detect_mode(message: str) -> str:
    msg = message.lower()
    for mode, keywords in _MODES.items():
        if any(kw in msg for kw in keywords):
            return mode
    return "general"


class TextAgent(BaseAgent):
    agent_name = "text"
    timeout    = 40

    def _system_prompt(self) -> str:
        return _SYSTEM

    async def _execute(self, ctx: AgentContext) -> AgentResult:
        mode     = _detect_mode(ctx.message)
        messages = self._build_messages(ctx)
        response = await self._llm.ainvoke(messages)
        content  = str(response.content)

        # Needs critic для анализа и редактуры — там важна точность
        needs_critic = mode in ("analyze", "edit")

        return AgentResult(
            success=True,
            content=content,
            agent_name=self.agent_name,
            needs_critic=needs_critic,
            metadata={"mode": mode},
        )
