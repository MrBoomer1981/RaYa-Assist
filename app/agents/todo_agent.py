"""
todo_agent.py — управление задачами через SQLite.

База данных — единственный источник правды.
Матрица Эйзенхауэра через поле priority (1=Q1, 2=Q2, 3=Q3/Q4).
"""
import json
import logging
import re

from langchain_core.messages import HumanMessage, SystemMessage

from app.agents.base_agent import AgentContext, AgentResult, BaseAgent
from app.database import delete_task, get_active_tasks, mark_task_done, save_task
from app.utils import strip_json

logger = logging.getLogger(__name__)

_SYSTEM = """\
Ты RaYa — личный менеджер пользователя. Управляешь задачами через базу данных.

Матрица Эйзенхауэра:
🔴 Q1 (priority=1) — Срочно и важно (дедлайн сегодня/завтра)
🟡 Q2 (priority=2) — Важно, не срочно (цели, развитие) ← дефолт
🟠 Q3 (priority=3) — Срочно, не важно (мелкие просьбы)
⚪ Q4 (priority=3) — Не срочно, не важно

Операции (используй XML-теги в ответе):
- Добавить: <tasks>[{"quadrant":"q2","tasks":["текст задачи"]}]</tasks>
- Добавить с дедлайном: <tasks deadline="ДД.ММ">[{"quadrant":"q1","tasks":["текст"]}]</tasks>
- Выполнить: <done>точный текст задачи</done>
- Удалить: <delete>точный текст задачи</delete>

При добавлении — определяй квадрант по контексту.
Если пользователь сказал "я сделал X" → <done>X</done>, отметь выполненным и спроси что дальше.
Обращайся к пользователю по имени (оно придёт в контексте).\
"""


class TodoAgent(BaseAgent):
    agent_name = "todo"
    timeout    = 30

    def _system_prompt(self) -> str:
        return _SYSTEM

    async def _execute(self, ctx: AgentContext) -> AgentResult:
        tasks_context = self._build_db_context(ctx.user_id)

        # Если просят показать задачи — сразу отдаём без LLM
        msg_lower = ctx.message.lower()
        if any(kw in msg_lower for kw in (
            "покажи задачи", "мои задачи", "список задач",
            "какие задачи", "что надо сделать", "что нужно сделать",
        )):
            content = tasks_context.replace("\n\nТекущие задачи:", "📋 **Текущие задачи:**")
            return AgentResult(
                success=True, content=content,
                agent_name=self.agent_name, needs_critic=False,
            )

        messages = [
            SystemMessage(content=_SYSTEM),
            *ctx.history,
            HumanMessage(content=ctx.message + tasks_context),
        ]

        response = await self._llm.ainvoke(messages)
        raw = str(response.content)

        # ── Добавить задачи ────────────────────────────────────────────────────
        tasks_match = re.search(r'<tasks(?:\s+deadline="([^"]*)")?>(.*?)</tasks>',
                                raw, re.DOTALL)
        if tasks_match:
            deadline = tasks_match.group(1) or ""
            await self._add_tasks(tasks_match.group(2).strip(), ctx.user_id, deadline)

        # ── Отметить выполненной ───────────────────────────────────────────────
        for match in re.finditer(r"<done>(.*?)</done>", raw, re.DOTALL):
            text = match.group(1).strip()
            tasks = get_active_tasks(ctx.user_id)
            for t in tasks:
                if text.lower() in t[1].lower():
                    mark_task_done(t[0], ctx.user_id)
                    logger.info("✅ Задача выполнена: '%s'", text[:50])
                    break

        # ── Удалить ────────────────────────────────────────────────────────────
        for match in re.finditer(r"<delete>(.*?)</delete>", raw, re.DOTALL):
            text = match.group(1).strip()
            tasks = get_active_tasks(ctx.user_id)
            for t in tasks:
                if text.lower() in t[1].lower():
                    delete_task(t[0], ctx.user_id)
                    logger.info("🗑️ Задача удалена: '%s'", text[:50])
                    break

        # Убираем теги из ответа
        reply = re.sub(r"\s*<(tasks|done|delete)[^>]*>.*?</(tasks|done|delete)>",
                       "", raw, flags=re.DOTALL).strip()

        return AgentResult(
            success=True, content=reply,
            agent_name=self.agent_name, needs_critic=False,
        )

    def _build_db_context(self, user_id: int) -> str:
        """Контекст задач из БД для LLM."""
        tasks = get_active_tasks(user_id)
        if not tasks:
            return "\n\nТекущих задач нет."
        emoji = {1: "🔴", 2: "🟡", 3: "🟠"}
        lines = ["\n\nТекущие задачи:"] + [
            f"  {emoji.get(t[2], '🟡')} {t[1]}" + (f" (до {t[3]})" if t[3] else "")
            for t in tasks
        ]
        return "\n".join(lines)

    async def _add_tasks(self, raw_json: str, user_id: int, deadline: str) -> None:
        """Добавляет задачи в БД."""
        try:
            groups = json.loads(strip_json(raw_json))
            if isinstance(groups, dict):
                groups = groups.get("groups", [groups])
        except Exception:
            logger.warning("todo: не смог распарсить tasks JSON: %s", raw_json[:100])
            return

        _Q_TO_PRIORITY = {"q1": 1, "q2": 2, "q3": 3, "q4": 3}

        for group in groups:
            quadrant = group.get("quadrant", "q2")
            tasks    = [t.strip() for t in group.get("tasks", []) if t.strip()]
            priority = _Q_TO_PRIORITY.get(quadrant, 2)
            for text in tasks:
                try:
                    task_id = save_task(user_id, text, priority, deadline)
                    logger.info("✅ [%s] Задача #%d: '%s'", quadrant.upper(), task_id, text[:50])
                except Exception as e:
                    logger.warning("todo: ошибка сохранения: %s", e)
