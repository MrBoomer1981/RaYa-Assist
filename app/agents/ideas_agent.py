"""
ideas_agent.py — агент генерации идей и творческого мышления.

Умеет:
- Брейнсторм: много идей без фильтра
- Обратный брейнсторм: как НЕ решить проблему → инверсия
- SCAMPER-техника: Substitute, Combine, Adapt, Modify, Put, Eliminate, Reverse
- Случайные аналогии: связывает задачу с несвязанными областями
- Devil's advocate: аргументы против идеи
- Развитие чужой идеи: берёт идею пользователя и расширяет
- «А что если»: провокационные гипотезы
"""
import logging

from app.agents.base_agent import AgentContext, AgentResult, BaseAgent

logger = logging.getLogger(__name__)

_SYSTEM = """\
Ты RaYa — генератор идей и нестандартных решений.

Режимы работы:

🌪️ БРЕЙНСТОРМ — много идей без оценки
Правило: количество важнее качества. 10-15 идей за раз.
Никакой самоцензуры. Даже абсурдные идеи — в список.

🔄 ОБРАТНЫЙ БРЕЙНСТОРМ — как гарантированно провалиться?
Потом инвертируешь: каждый способ провала → способ успеха.

🔨 SCAMPER — систематическая трансформация идеи:
S — что можно заменить?
C — что можно объединить?
A — что можно адаптировать из другой области?
M — что можно усилить или уменьшить?
P — как ещё это можно использовать?
E — что можно убрать?
R — что можно перевернуть с ног на голову?

🎲 СЛУЧАЙНЫЕ АНАЛОГИИ — связываешь задачу с неожиданной областью
Пример: "Как стартап похож на джаз-группу?"

😈 DEVIL'S ADVOCATE — 5 лучших аргументов ПРОТИВ идеи

💡 РАЗВИТИЕ ИДЕИ — берёшь зерно и выращиваешь в полноценную концепцию

❓ А ЧТО ЕСЛИ — провокационные гипотезы: "А что если убрать X?", "А что если сделать наоборот?"

Как работаешь:
- Определяешь какой режим нужен (или комбинируешь)
- Не оцениваешь идеи — это задача пользователя
- Предлагаешь конкретные, не расплывчатые идеи
- В конце — одна идея которую сама считаешь самой интересной, с коротким объяснением почему

Обращайся к пользователю по имени. Энергичный, вдохновляющий тон — ты любишь эту работу."""

_TECHNIQUES = {
    "scamper":      ("scamper", "scamper-"),
    "reverse":      ("обратный брейнсторм", "как провалиться", "анти-"),
    "analogy":      ("аналогия", "как это похоже", "сравни с"),
    "devil":        ("devil", "против идеи", "недостатки", "минусы идеи"),
    "what_if":      ("а что если", "что если убрать", "что если наоборот"),
    "brainstorm":   ("идеи", "придумай", "брейнсторм", "варианты", "способы"),
}


def _detect_technique(message: str) -> str:
    msg = message.lower()
    for technique, keywords in _TECHNIQUES.items():
        if any(kw in msg for kw in keywords):
            return technique
    return "brainstorm"


class IdeasAgent(BaseAgent):
    agent_name = "ideas"
    timeout    = 40

    def _system_prompt(self) -> str:
        return _SYSTEM

    async def _execute(self, ctx: AgentContext) -> AgentResult:
        technique = _detect_technique(ctx.message)
        messages  = self._build_messages(ctx)
        response  = await self._llm.ainvoke(messages)
        content   = str(response.content)

        # Сохраняем в Obsidian фоново
        from app.config import settings as _cfg
        if _cfg.obsidian_enabled:
            try:
                from app.services.obsidian_tasks import add_idea
                import asyncio as _aio
                _t = _aio.create_task(add_idea(ctx.message[:80], content[:400]))
                self.__dict__.setdefault('_bg', set()).add(_t)
                _t.add_done_callback(self.__dict__['_bg'].discard)
            except Exception as _e:
                logger.debug('ideas obsidian: %s', _e)

        return AgentResult(
            success=True,
            content=content,
            agent_name=self.agent_name,
            needs_critic=False,  # идеи не критикуем — это убивает творчество
            metadata={"technique": technique},
        )
