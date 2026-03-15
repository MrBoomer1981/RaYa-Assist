"""
todo_agent.py — управление задачами.

Obsidian — единственный источник правды.
4 файла: Q1.md, Q2.md, Q3.md, Q4.md

Добавление → определяет квадрант → пишет в нужный файл.
Просмотр   → читает все 4 файла → собирает в одно сообщение.
Выполнение → меняет [ ] на [x] в файле.
Удаление   → удаляет строку из файла.
"""
import json
import logging
import re

from langchain_core.messages import HumanMessage, SystemMessage

from app.agents.base_agent import AgentContext, AgentResult, BaseAgent
from app.database import delete_task, get_active_tasks, mark_task_done, save_task
from app.integrations.obsidian import (
    QUADRANTS, add_tasks, delete_task_obsidian,
    format_all_tasks, get_all_tasks,
    mark_task_done_obsidian, vault_available,
)
from app.utils import strip_json

logger = logging.getLogger(__name__)

_SYSTEM = """\
Ты RaYa — личный ассистент Сократа. Управляешь задачами через Obsidian.

Задачи хранятся в 4 файлах по матрице Эйзенхауэра:
🔴 Q1 — Срочно и важно (дедлайн сегодня/завтра, критично)
🟡 Q2 — Важно, не срочно (цели, развитие, планирование)
🟠 Q3 — Срочно, не важно (мелкие просьбы, рутина)
⚪ Q4 — Не срочно, не важно (когда-нибудь)

Что умеешь:
- Добавлять задачи → определяй квадрант автоматически
- Показывать список → собирай все задачи в одно сообщение
- Отмечать выполненными → по тексту задачи
- Удалять → по тексту задачи

Когда добавляешь — верни JSON:
<tasks>{"groups": [{"quadrant": "q1", "tasks": ["задача"]}, {"quadrant": "q2", "tasks": ["задача2"]}]}</tasks>

Когда отмечаешь выполненной:
<done>точный текст задачи</done>

Когда удаляешь:
<delete>точный текст задачи</delete>

Обращайся только "Сократ". Отвечай кратко.\
"""


class TodoAgent(BaseAgent):
    agent_name = "todo"
    timeout    = 35

    def _system_prompt(self) -> str:
        return _SYSTEM

    async def _execute(self, ctx: AgentContext) -> AgentResult:
        # Читаем задачи из Obsidian если доступен, иначе из БД
        if vault_available():
            tasks_context = self._build_obsidian_context()
        else:
            tasks_context = self._build_db_context(ctx.user_id)

        messages = [
            SystemMessage(content=_SYSTEM),
            *ctx.history,
            HumanMessage(content=ctx.message + tasks_context),
        ]

        response = await self._llm.ainvoke(messages)
        raw      = str(response.content)
        metadata = {}

        # ── Добавить задачи ────────────────────────────────────────────────────
        tasks_match = re.search(r"<tasks>(.*?)</tasks>", raw, re.DOTALL)
        if tasks_match:
            await self._add_tasks(tasks_match.group(1).strip(), ctx.user_id, metadata)

        # ── Отметить выполненной ───────────────────────────────────────────────
        for match in re.finditer(r"<done>(.*?)</done>", raw, re.DOTALL):
            text = match.group(1).strip()
            if vault_available():
                mark_task_done_obsidian(text)
            else:
                # Ищем в БД по тексту
                tasks = get_active_tasks(ctx.user_id)
                for t in tasks:
                    if text.lower() in t[1].lower():
                        mark_task_done(t[0], ctx.user_id)
                        break
            logger.info("✅ Задача выполнена: '%s'", text[:50])

        # ── Удалить ────────────────────────────────────────────────────────────
        for match in re.finditer(r"<delete>(.*?)</delete>", raw, re.DOTALL):
            text = match.group(1).strip()
            if vault_available():
                delete_task_obsidian(text)
            else:
                tasks = get_active_tasks(ctx.user_id)
                for t in tasks:
                    if text.lower() in t[1].lower():
                        delete_task(t[0], ctx.user_id)
                        break
            logger.info("🗑️ Задача удалена: '%s'", text[:50])

        # Убираем теги из ответа
        reply = re.sub(r"\s*<(tasks|done|delete)>.*?</(tasks|done|delete)>",
                       "", raw, flags=re.DOTALL).strip()

        # Если просили показать задачи — отдаём форматированный список
        msg_lower = ctx.message.lower()
        if any(kw in msg_lower for kw in ("покажи задачи", "мои задачи", "список задач",
                                           "какие задачи", "что надо сделать")):
            if vault_available():
                return AgentResult(
                    success=True, content=format_all_tasks(),
                    agent_name=self.agent_name, needs_critic=False,
                )

        return AgentResult(
            success=True, content=reply,
            agent_name=self.agent_name, needs_critic=False,
            metadata=metadata,
        )

    def _build_obsidian_context(self) -> str:
        """Контекст из Obsidian для LLM."""
        try:
            all_tasks = get_all_tasks()
            lines = ["\n\nТекущие задачи в Obsidian:"]
            has_tasks = False
            for q_key in ("q1", "q2", "q3", "q4"):
                data   = all_tasks[q_key]
                active = [t for t in data["tasks"] if not t["done"]]
                if active:
                    lines.append(f"{data['emoji']} {data['title']}:")
                    for t in active:
                        lines.append(f"  - {t['text']}")
                    has_tasks = True
            return "\n".join(lines) if has_tasks else "\n\nЗадач нет."
        except Exception:
            return "\n\nЗадач нет."

    def _build_db_context(self, user_id: int) -> str:
        """Фоллбек — контекст из БД."""
        tasks = get_active_tasks(user_id)
        if not tasks:
            return "\n\nТекущих задач нет."
        emoji = {1: "🔴", 2: "🟡", 3: "🟢"}
        lines = ["\n\nТекущие задачи:"] + [
            f"  - {emoji.get(t[2],'🟡')} {t[1]}" + (f" (до {t[3]})" if t[3] else "")
            for t in tasks
        ]
        return "\n".join(lines)

    async def _add_tasks(self, raw_json: str, user_id: int, metadata: dict) -> None:
        """Добавляет задачи в Obsidian и БД."""
        try:
            data   = json.loads(strip_json(raw_json))
            groups = data.get("groups", [])
        except Exception:
            logger.warning("todo: не смог распарсить tasks JSON: %s", raw_json[:100])
            return

        _Q_TO_PRIORITY = {"q1": 1, "q2": 2, "q3": 3, "q4": 3}

        for group in groups:
            quadrant = group.get("quadrant", "q2")
            tasks    = [t.strip() for t in group.get("tasks", []) if t.strip()]
            if not tasks:
                continue

            # Obsidian
            if vault_available():
                add_tasks(tasks, quadrant=quadrant)

            # БД (для напоминаний)
            priority = _Q_TO_PRIORITY.get(quadrant, 2)
            for text in tasks:
                try:
                    task_id = save_task(user_id, text, priority, "")
                    metadata.setdefault("tasks_added", []).append(task_id)
                    logger.info("✅ [%s] Задача #%d: '%s'", quadrant.upper(), task_id, text[:50])
                except Exception as e:
                    logger.warning("todo: ошибка сохранения в БД: %s", e)
