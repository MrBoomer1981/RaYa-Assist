"""
vault_tool.py — инструмент Obsidian vault для Anthropic tool use.

Оптимизации:
- Поддержка нескольких tool_calls за один ответ (параллельно)
- Правильный agentic loop: tool_calls → ToolMessage → финальный ответ
- Quadrant определяется моделью через описание, не хардкодом
- Lazy import obsidian — не грузим если vault недоступен
"""
import logging

logger = logging.getLogger(__name__)

# ── Schema ────────────────────────────────────────────────────────────────────

VAULT_TOOL = {
    "name": "vault",
    "description": (
        "Obsidian vault Сократа. Вызывай ТОЛЬКО когда нужно:\n"
        "• сохранить задачу / отметить выполненной / удалить\n"
        "• записать в дневник (личное, события дня)\n"
        "• добавить идею/концепцию в базу знаний (zettel)\n"
        "• создать заметку или план\n"
        "• найти что-то в vault\n"
        "НЕ вызывай для обычного разговора, вопросов, объяснений."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "op": {
                "type": "string",
                "enum": [
                    "add_task",    # добавить задачу
                    "done_task",   # отметить выполненной (по тексту)
                    "delete_task", # удалить задачу (по тексту)
                    "list_tasks",  # показать все задачи
                    "write_diary", # запись в дневник
                    "add_zettel",  # в базу знаний (атомарная идея)
                    "create_note", # структурированная заметка
                    "create_plan", # план
                    "search",      # поиск по vault
                    "cleanup",     # удалить лишние файлы
                ],
                "description": "Операция"
            },
            "text": {
                "type": "string",
                "description": (
                    "Текст для операции. "
                    "Для done_task/delete_task — точный текст задачи из vault. "
                    "Для list_tasks/cleanup — оставь пустым."
                )
            },
            "quadrant": {
                "type": "string",
                "enum": ["q1", "q2", "q3", "q4"],
                "description": (
                    "Только для add_task. Матрица Эйзенхауэра:\n"
                    "q1 — срочно И важно (дедлайн сегодня/завтра)\n"
                    "q2 — важно, не срочно (цели, развитие) ← дефолт\n"
                    "q3 — срочно, не важно (мелкие просьбы)\n"
                    "q4 — ни то ни другое"
                )
            },
            "plan_horizon": {
                "type": "string",
                "enum": ["short", "long"],
                "description": "Для create_plan: short (≤2 нед) или long"
            },
        },
        "required": ["op"]
    }
}


# ── Executor ──────────────────────────────────────────────────────────────────

async def run_vault_op(op: str, text: str = "", quadrant: str = "q2",
                       plan_horizon: str = "short", user_id: int = 0) -> str:
    """Выполняет одну операцию vault. Возвращает строку-результат для модели."""
    try:
        from app.integrations.obsidian import (
            QUADRANTS, add_tasks, add_zettel, cleanup_vault, create_note,
            create_plan, delete_task_obsidian, format_all_tasks,
            list_zettel_titles, mark_task_done_obsidian, search_vault,
            update_zettel, vault_available, write_diary,
        )
        from app.database import delete_task, get_active_tasks, mark_task_done, save_task

        if op != "list_tasks" and op != "cleanup" and not vault_available():
            return "vault недоступен — OBSIDIAN_VAULT_PATH не задан"

        if op == "add_task":
            if not text:
                return "ошибка: текст задачи пустой"
            if quadrant not in QUADRANTS:
                quadrant = "q2"
            add_tasks([text], quadrant=quadrant)
            if user_id:
                _prio = {"q1": 1, "q2": 2, "q3": 3, "q4": 3}
                save_task(user_id, text, _prio.get(quadrant, 2), "")
            q = QUADRANTS[quadrant]
            logger.info("vault add_task [%s]: '%s'", quadrant, text[:50])
            return f"добавлено в {q['emoji']} {q['title']}"

        elif op == "done_task":
            if not text:
                return "ошибка: текст задачи пустой"
            found = mark_task_done_obsidian(text)
            if user_id:
                for t in get_active_tasks(user_id):
                    if text.lower() in t[1].lower():
                        mark_task_done(t[0], user_id)
                        break
            logger.info("vault done_task: '%s' found=%s", text[:50], found)
            return f"выполнено: «{text}»" if found else f"не нашла задачу: «{text}»"

        elif op == "delete_task":
            if not text:
                return "ошибка: текст задачи пустой"
            found = delete_task_obsidian(text)
            if user_id:
                for t in get_active_tasks(user_id):
                    if text.lower() in t[1].lower():
                        delete_task(t[0], user_id)
                        break
            logger.info("vault delete_task: '%s' found=%s", text[:50], found)
            return f"удалено: «{text}»" if found else f"не нашла задачу: «{text}»"

        elif op == "list_tasks":
            return format_all_tasks()

        elif op == "write_diary":
            if not text:
                return "ошибка: текст записи пустой"
            path = write_diary(text)
            logger.info("vault write_diary: %s", path)
            return f"записано в дневник: {path}"

        elif op == "add_zettel":
            if not text:
                return "ошибка: текст пустой"
            # Быстрый dedup по словам без LLM
            titles = list_zettel_titles()
            text_words = set(w for w in text.lower().split() if len(w) > 3)
            for entry in titles[-30:]:
                title_words = set(w for w in entry["title"].lower().split() if len(w) > 3)
                tag_words   = set(w for tag in entry["tags"] for w in tag.lower().split())
                overlap = len(text_words & (title_words | tag_words))
                if overlap >= 3:
                    path = update_zettel(entry["id"], text)
                    logger.info("vault update_zettel: %s", entry["id"])
                    return f"дополнила карточку «{entry['title']}»"
            path = add_zettel(text.split("\n")[0][:60], text)
            logger.info("vault add_zettel: '%s'", text[:40])
            return f"добавила в базу знаний: {path}"

        elif op == "create_note":
            if not text:
                return "ошибка: текст пустой"
            title = text.split("\n")[0][:50]
            path  = create_note(title, text)
            logger.info("vault create_note: %s", path)
            return f"заметка создана: {path}"

        elif op == "create_plan":
            if not text:
                return "ошибка: текст пустой"
            if plan_horizon not in ("short", "long"):
                plan_horizon = "short"
            title  = text.split("\n")[0][:50]
            path   = create_plan(title, text, plan_horizon)
            folder = "Краткосрочные" if plan_horizon == "short" else "Долгосрочные"
            logger.info("vault create_plan [%s]: %s", plan_horizon, path)
            return f"план в {folder}: {path}"

        elif op == "search":
            if not text:
                return "ошибка: запрос пустой"
            results = search_vault(text)
            if not results:
                return f"ничего не найдено по «{text}»"
            lines = [f"найдено {len(results)} по «{text}»:"]
            for r in results[:5]:
                lines.append(f"• {r['path']}: {r['snippet'][:100]}")
            return "\n".join(lines)

        elif op == "cleanup":
            result  = cleanup_vault()
            deleted = result.get("deleted", [])
            if not deleted:
                return "vault чист"
            return f"удалено {len(deleted)}: {', '.join(deleted)}"

        else:
            return f"неизвестная операция: {op}"

    except Exception as e:
        logger.exception("vault_tool op=%s", op)
        return f"ошибка: {e}"


async def process_tool_calls(tool_calls: list, user_id: int) -> list[dict]:
    """
    Параллельно обрабатывает все tool_calls.
    Возвращает список {tool_call_id, result} для сборки ToolMessages.
    """
    import asyncio
    results = []

    async def _one(tc: dict) -> dict:
        if tc.get("name") != "vault":
            return {"tool_call_id": tc.get("id", ""), "result": "unknown tool"}
        args   = tc.get("args", {})
        result = await run_vault_op(
            op=args.get("op", ""),
            text=args.get("text", ""),
            quadrant=args.get("quadrant", "q2"),
            plan_horizon=args.get("plan_horizon", "short"),
            user_id=user_id,
        )
        return {"tool_call_id": tc.get("id", "call_0"), "result": result}

    # Параллельный запуск всех операций
    results = await asyncio.gather(*[_one(tc) for tc in tool_calls])
    return list(results)
