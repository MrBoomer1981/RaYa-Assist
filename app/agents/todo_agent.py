"""
todo_agent.py — агент управления задачами.

Сохраняет задачи в БД (для напоминаний) И в Obsidian (матрица Эйзенхауэра).
Удаление по имени задачи — не требует знать ID.
"""
import json
import logging
import re

from langchain_core.messages import HumanMessage, SystemMessage

from app.agents.base_agent import AgentContext, AgentResult, BaseAgent
from app.database import delete_task, get_active_tasks, mark_task_done, save_task

logger = logging.getLogger(__name__)

_SYSTEM = """\
Ты RaYa — личный ассистент Сократа. Помогаешь управлять задачами.

Умеешь:
- Добавлять задачи с приоритетом и дедлайном
- Показывать список текущих задач
- Отмечать задачи выполненными
- Удалять задачи по названию или номеру

Приоритеты:
- Высокий (1) 🔴 — срочно и важно (Q1)
- Средний (2) 🟡 — важно, не срочно (Q2, дефолт)
- Низкий (3) 🟢 — не срочно, не важно (Q4)

Когда добавляешь задачу — верни JSON тег:
<task>{"text": "...", "priority": 1-3, "due_date": "YYYY-MM-DD или пусто"}</task>

Когда отмечаешь выполненной — верни:
<done>ID_задачи</done>

Когда удаляешь — верни:
<delete>ID_задачи</delete>

Если пользователь называет задачу по тексту (не по ID) — найди её в списке и используй ID.
Если просто показать список — просто отвечай текстом.
Обращайся только "Сократ". Тон дружелюбный, без лишних слов.\
"""

_PRIORITY_EMOJI = {1: "🔴", 2: "🟡", 3: "🟢"}

# Маппинг приоритет → квадрант Эйзенхауэра
_PRIORITY_TO_QUADRANT = {1: "q1", 2: "q2", 3: "q4"}

# Промпт для LLM чтобы определить квадрант по тексту задачи
_EISENHOWER_PROMPT = """\
Определи квадрант матрицы Эйзенхауэра для каждой задачи.

Задачи: {tasks}

Квадранты:
- q1: срочно И важно (дедлайн сегодня/завтра, критично)
- q2: важно, не срочно (развитие, планирование)
- q3: срочно, не важно (чужие просьбы, рутина)
- q4: не срочно и не важно (мелочи, развлечения)

JSON (только JSON):
{{"groups": [{{"quadrant": "q1", "tasks": ["задача"]}}, {{"quadrant": "q2", "tasks": ["задача2"]}}]}}"""


class TodoAgent(BaseAgent):
    agent_name = "todo"
    timeout    = 35

    def _system_prompt(self) -> str:
        return _SYSTEM

    async def _execute(self, ctx: AgentContext) -> AgentResult:
        # Загружаем текущие задачи
        tasks = get_active_tasks(ctx.user_id)
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
        raw      = str(response.content)
        metadata = {}
        added_tasks = []

        # ── Добавить задачу ────────────────────────────────────────────────────
        for match in re.finditer(r"<task>(.*?)</task>", raw, re.DOTALL):
            try:
                data    = json.loads(match.group(1).strip())
                text    = data.get("text", "").strip()
                priority = int(data.get("priority", 2))
                due_date = data.get("due_date", "")
                if not text:
                    continue
                task_id = save_task(ctx.user_id, text, priority, due_date)
                metadata.setdefault("tasks_added", []).append(task_id)
                added_tasks.append({"text": text, "priority": priority})
                logger.info("✅ Задача #%d добавлена | user_id=%s", task_id, ctx.user_id)
            except Exception as e:
                logger.warning("todo: ошибка парсинга task: %s", e)

        # ── Также пишем в Obsidian с матрицей Эйзенхауэра ─────────────────────
        if added_tasks:
            await self._sync_to_obsidian(added_tasks)

        # ── Отметить выполненной ───────────────────────────────────────────────
        for match in re.finditer(r"<done>(\d+)</done>", raw):
            task_id = int(match.group(1))
            ok = mark_task_done(task_id, ctx.user_id)
            if ok:
                metadata["task_done"] = task_id
                logger.info("✅ Задача #%d выполнена | user_id=%s", task_id, ctx.user_id)

        # ── Удалить ────────────────────────────────────────────────────────────
        for match in re.finditer(r"<delete>(\d+)</delete>", raw):
            task_id = int(match.group(1))
            ok = delete_task(task_id, ctx.user_id)
            if ok:
                metadata["task_deleted"] = task_id
                logger.info("🗑️ Задача #%d удалена | user_id=%s", task_id, ctx.user_id)

        # Убираем теги из ответа
        reply = re.sub(
            r"\s*<(task|done|delete)>.*?</(task|done|delete)>",
            "", raw, flags=re.DOTALL
        ).strip()

        return AgentResult(
            success=True, content=reply,
            agent_name=self.agent_name, needs_critic=False,
            metadata=metadata,
        )

    async def _sync_to_obsidian(self, tasks: list[dict]) -> None:
        """Синхронизирует задачи в Obsidian с разбивкой по квадрантам."""
        try:
            from app.integrations.obsidian import QUADRANTS, add_tasks, vault_available
            from app.utils import strip_json

            if not vault_available():
                return

            # Определяем квадранты через LLM
            task_texts = [t["text"] for t in tasks]
            prompt     = _EISENHOWER_PROMPT.format(tasks="\n".join(f"- {t}" for t in task_texts))
            resp       = await self._llm.ainvoke([HumanMessage(content=prompt)])
            raw        = strip_json(str(resp.content))

            try:
                groups = json.loads(raw).get("groups", [])
            except Exception:
                # Фоллбек — по приоритету
                groups = []
                for t in tasks:
                    q = _PRIORITY_TO_QUADRANT.get(t.get("priority", 2), "q2")
                    existing = next((g for g in groups if g["quadrant"] == q), None)
                    if existing:
                        existing["tasks"].append(t["text"])
                    else:
                        groups.append({"quadrant": q, "tasks": [t["text"]]})

            for group in groups:
                quadrant = group.get("quadrant", "q2")
                gtasks   = group.get("tasks", [])
                if gtasks:
                    add_tasks(gtasks, quadrant=quadrant)
                    q_info = QUADRANTS.get(quadrant, QUADRANTS["q2"])
                    logger.info(
                        "📁 Obsidian %s: %d задач", q_info["name"], len(gtasks)
                    )
        except Exception:
            logger.warning("todo: не удалось синхронизировать с Obsidian", exc_info=True)
