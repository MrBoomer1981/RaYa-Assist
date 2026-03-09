"""
todo_agent.py — агент управления задачами.
Создаёт, показывает, завершает и удаляет задачи.
Понимает приоритеты и дедлайны из естественного языка.
"""
import logging

from langchain_core.messages import HumanMessage, SystemMessage

from app.agents.base_agent import AgentContext, AgentResult, BaseAgent
from app.database import (
    delete_task, get_active_tasks, mark_task_done, save_task,
)

logger = logging.getLogger(__name__)

_SYSTEM = """\
Ты RaYa — личный ассистент Сократа. Помогаешь управлять задачами.

Умеешь:
- Добавлять задачи с приоритетом (высокий/средний/низкий) и дедлайном
- Показывать список текущих задач красиво и структурированно
- Отмечать задачи выполненными
- Удалять задачи

Приоритеты:
- Высокий (1) 🔴 — срочно, важно
- Средний (2) 🟡 — обычная задача (дефолт)
- Низкий (3) 🟢 — когда-нибудь

Формат ответа когда нужно добавить задачу:
<task>{"text": "...", "priority": 1-3, "due_date": "YYYY-MM-DD или пусто"}</task>

Формат когда нужно отметить выполненной:
<done>ID_задачи</done>

Формат когда нужно удалить:
<delete>ID_задачи</delete>

Если просто показать список — просто отвечай текстом.
Обращайся только "Сократ". Тон дружелюбный, без лишних слов."""

_PRIORITY_EMOJI = {1: "🔴", 2: "🟡", 3: "🟢"}
_PRIORITY_NAME  = {1: "высокий", 2: "средний", 3: "низкий"}


class TodoAgent(BaseAgent):
    agent_name = "todo"
    timeout = 30

    def _system_prompt(self) -> str:
        return _SYSTEM

    async def _execute(self, ctx: AgentContext) -> AgentResult:
        import json, re

        # Загружаем текущие задачи для контекста
        tasks = get_active_tasks(ctx.user_id)
        tasks_context = ""
        if tasks:
            lines = [
                f"[#{t[0]}] {_PRIORITY_EMOJI.get(t[2], '🟡')} {t[1]}"
                + (f" (до {t[3]})" if t[3] else "")
                for t in tasks
            ]
            tasks_context = "\n\nТекущие задачи:\n" + "\n".join(lines)
        else:
            tasks_context = "\n\nТекущих задач нет."

        messages = [
            SystemMessage(content=_SYSTEM),
            *ctx.history,
            HumanMessage(content=ctx.message + tasks_context),
        ]

        response = await self._llm.ainvoke(messages)
        raw = str(response.content)

        metadata = {}

        # Парсим команды модели
        # Добавить задачу
        task_match = re.search(r"<task>(.*?)</task>", raw, re.DOTALL)
        if task_match:
            try:
                data = json.loads(task_match.group(1).strip())
                task_id = save_task(
                    ctx.user_id,
                    data.get("text", ""),
                    int(data.get("priority", 2)),
                    data.get("due_date", ""),
                )
                metadata["task_added"] = task_id
                logger.info("✅ Задача #%d добавлена | user_id=%s", task_id, ctx.user_id)
            except Exception as e:
                logger.warning("todo: ошибка парсинга task: %s", e)

        # Отметить выполненной
        done_match = re.search(r"<done>(\d+)</done>", raw)
        if done_match:
            task_id = int(done_match.group(1))
            ok = mark_task_done(task_id, ctx.user_id)
            metadata["task_done"] = task_id if ok else None
            if ok:
                logger.info("✅ Задача #%d выполнена | user_id=%s", task_id, ctx.user_id)

        # Удалить
        del_match = re.search(r"<delete>(\d+)</delete>", raw)
        if del_match:
            task_id = int(del_match.group(1))
            ok = delete_task(task_id, ctx.user_id)
            metadata["task_deleted"] = task_id if ok else None

        # Убираем теги из ответа пользователю
        reply = re.sub(r"\s*<(task|done|delete)>.*?</(task|done|delete)>", "", raw, flags=re.DOTALL).strip()

        return AgentResult(
            success=True,
            content=reply,
            agent_name=self.agent_name,
            needs_critic=False,
            metadata=metadata,
        )
