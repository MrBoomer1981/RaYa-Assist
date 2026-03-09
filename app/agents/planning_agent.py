"""
planning_agent.py — агент планирования и декомпозиции.

Умеет:
- Декомпозировать большую задачу на шаги
- Строить план с дедлайнами и приоритетами
- Оценивать реалистичность плана
- Находить зависимости между задачами
- Предлагать метрики успеха
- Анализировать риски
- Помогать с тайм-менеджментом
"""
import logging
import re
import json

from langchain_core.messages import HumanMessage, SystemMessage

from app.agents.base_agent import AgentContext, AgentResult, BaseAgent
from app.database import save_task

logger = logging.getLogger(__name__)

_SYSTEM = """\
Ты RaYa — эксперт по планированию и декомпозиции задач.

Что умеешь:

📋 ДЕКОМПОЗИЦИЯ — разбиваешь большую задачу на конкретные шаги
Принципы:
- Каждый шаг — одно действие, не процесс
- Шаг выполним за 1-4 часа (если не указано иное)
- Чёткий результат у каждого шага

📅 ПЛАН С ДЕДЛАЙНАМИ — шаги + временны́е рамки
- Реалистичные оценки, не оптимистичные
- Учитываешь зависимости: что блокирует что
- Добавляешь буфер 20% на непредвиденное

⚠️ АНАЛИЗ РИСКОВ — что может пойти не так?
- Топ-3 риска с вероятностью и митигацией
- Только реальные, не надуманные

📊 МЕТРИКИ УСПЕХА — как понять что цель достигнута?
- Конкретные, измеримые критерии
- Промежуточные чекпоинты

🔍 РЕВЬЮ ПЛАНА — оцениваешь готовый план
- Что реалистично, что нет
- Чего не хватает
- Главная угроза

Формат декомпозиции — если нужно сохранить шаги как задачи:
<save_tasks>
[
  {"text": "название задачи", "priority": 1-3, "due_date": "YYYY-MM-DD или пусто"},
  ...
]
</save_tasks>

Обращайся только "Сократ". Конкретно, без воды. Уважаешь время."""

_PRIORITY_WORDS = {
    1: ["срочно", "критично", "блокер", "сегодня"],
    3: ["когда-нибудь", "потом", "низкий", "не горит"],
}


def _infer_priority(text: str) -> int:
    t = text.lower()
    for p, words in _PRIORITY_WORDS.items():
        if any(w in t for w in words):
            return p
    return 2


class PlanningAgent(BaseAgent):
    agent_name = "planning"
    timeout    = 45

    def _system_prompt(self) -> str:
        return _SYSTEM

    async def _execute(self, ctx: AgentContext) -> AgentResult:
        messages = self._build_messages(ctx)
        response = await self._llm.ainvoke(messages)
        raw      = str(response.content)

        saved_tasks = []
        metadata    = {}

        # Парсим <save_tasks> если модель решила сохранить шаги
        match = re.search(r"<save_tasks>(.*?)</save_tasks>", raw, re.DOTALL)
        if match:
            try:
                tasks_data = json.loads(match.group(1).strip())
                for t in tasks_data:
                    if not t.get("text"):
                        continue
                    task_id = save_task(
                        ctx.user_id,
                        t["text"],
                        int(t.get("priority", _infer_priority(t["text"]))),
                        t.get("due_date", ""),
                    )
                    saved_tasks.append(task_id)
                    logger.info(
                        "📋 Задача #%d сохранена из плана | user_id=%s",
                        task_id, ctx.user_id,
                    )
                metadata["saved_tasks"] = saved_tasks
            except (json.JSONDecodeError, KeyError, TypeError) as e:
                logger.warning("planning: ошибка парсинга задач: %s", e)

        # Убираем тег из ответа
        reply = re.sub(
            r"\s*<save_tasks>.*?</save_tasks>", "", raw, flags=re.DOTALL
        ).strip()

        if saved_tasks:
            reply += f"\n\n✅ Сохранила {len(saved_tasks)} задач в список."

        return AgentResult(
            success=True,
            content=reply,
            agent_name=self.agent_name,
            needs_critic=False,
            metadata=metadata,
        )
