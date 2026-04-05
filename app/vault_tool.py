"""
vault_tool.py — универсальный инструмент Obsidian vault для tool use.
"""
import logging

logger = logging.getLogger(__name__)

VAULT_TOOL = {
    "name": "vault",
    "description": (
        "Obsidian vault пользователя. Вызывай ТОЛЬКО когда нужно:\n"
        "• сохранить задачу / отметить выполненной / удалить\n"
        "• записать в дневник (личное, события дня)\n"
        "• добавить идею/концепцию в базу знаний (zettel)\n"
        "• создать заметку или план\n"
        "• найти что-то в vault / прочитать заметку\n"
        "• показать список задач или файлов\n"
        "• очистить выполненные задачи\n"
        "НЕ вызывай для обычного разговора, вопросов, объяснений."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "op": {
                "type": "string",
                "enum": [
                    "add_task",       # добавить задачу
                    "done_task",      # отметить выполненной (fuzzy match)
                    "delete_task",    # удалить задачу (fuzzy match)
                    "list_tasks",     # показать все активные задачи
                    "clear_done",     # удалить все выполненные задачи
                    "write_diary",    # запись в дневник
                    "add_zettel",     # в базу знаний (атомарная идея)
                    "create_note",    # структурированная заметка
                    "create_plan",    # план
                    "read_note",      # прочитать заметку по названию/запросу
                    "search",         # поиск по vault (fulltext + по тегам #тег)
                    "list_files",     # список файлов в папке
                    "stats",          # статистика vault
                    "cleanup",        # удалить лишние файлы
                    "move_task",      # переместить задачу в другой квадрант
                    "overdue_tasks",  # показать задачи с дедлайном сегодня/просроченные
                    "week_plan",      # план задач на неделю
                    "batch_done",     # отметить несколько задач выполненными
                    "diary_context",  # показать контекст дневника
                    "undo_task",      # вернуть выполненную задачу в активные
                    "add_event",      # добавить событие в календарь
                    "list_events",    # показать события (date или upcoming)
                    "delete_event",   # удалить событие по id или названию
                    "add_event",      # добавить событие в календарь
                    "get_events",     # события на дату или ближайшие
                    "delete_event",   # удалить событие по id
                ],
                "description": "Операция"
            },
            "text": {
                "type": "string",
                "description": (
                    "Текст для операции.\n"
                    "done_task/delete_task — можно неточный текст задачи (fuzzy match).\n"
                    "search — запрос или #тег для поиска по тегу.\n"
                    "list_tasks/clear_done/stats/cleanup — оставь пустым."
                )
            },
            "quadrant": {
                "type": "string",
                "enum": ["q1", "q2", "q3", "q4"],
                "description": (
                    "Только для add_task:\n"
                    "q1 — срочно И важно (дедлайн сегодня/завтра)\n"
                    "q2 — важно, не срочно ← дефолт\n"
                    "q3 — срочно, не важно\n"
                    "q4 — ни то ни другое"
                )
            },
            "plan_horizon": {
                "type": "string",
                "enum": ["short", "long"],
                "description": "Для create_plan: short (≤2 нед) или long"
            },
            "folder": {
                "type": "string",
                "description": "Для list_files: папка (Заметки / Zettelkasten / Дневник / Планы)"
            },
            "deadline": {
                "type": "string",
                "description": "Дедлайн для add_task, формат: ДД.ММ или ДД.ММ.ГГГГ. Пример: 20.03"
            },
            "target_quadrant": {
                "type": "string",
                "enum": ["q1", "q2", "q3", "q4"],
                "description": "Для move_task: целевой квадрант"
            },
            "event_date": {
                "type": "string",
                "description": "Дата события YYYY-MM-DD. Для list_events: конкретная дата или пусто = ближайшие"
            },
            "event_time_start": {"type": "string", "description": "Время начала HH:MM"},
            "event_time_end":   {"type": "string", "description": "Время конца HH:MM"},
            "event_color": {
                "type": "string",
                "enum": ["blue", "green", "red", "orange", "purple"],
                "description": "Цвет события"
            },
            "event_date": {
                "type": "string",
                "description": "Дата события: YYYY-MM-DD. Сегодня если не указана."
            },
            "event_time": {
                "type": "string",
                "description": "Время начала: HH:MM. Пусто = весь день."
            },
            "event_time_end": {
                "type": "string",
                "description": "Время окончания: HH:MM."
            },
            "event_color": {
                "type": "string",
                "enum": ["blue", "green", "red", "orange", "purple"],
                "description": "Цвет события. blue по умолчанию."
            },
            "event_id": {
                "type": "integer",
                "description": "ID события для delete_event"
            },
        },
        "required": ["op"]
    }
}


async def run_vault_op(op: str, text: str = "", quadrant: str = "q2",
                       plan_horizon: str = "short", folder: str = "Заметки",
                       deadline: str = "", target_quadrant: str = "q2",
                       event_date: str = "", event_time: str = "",
                       event_time_end: str = "", event_color: str = "blue",
                       event_id: int = 0,
                       user_id: int = 0) -> str:
    try:
        from app.integrations.obsidian import (
            QUADRANTS, add_task_with_deadline, add_tasks, add_zettel,
            batch_add_tasks, cleanup_vault, clear_done_tasks, create_note,
            create_plan, delete_task_obsidian, format_all_tasks,
            get_diary_context, get_overdue_tasks, get_week_plan,
            list_files, list_zettel_titles, mark_multiple_done,
            mark_task_done_obsidian, move_task, read_note,
            search_vault, update_zettel, vault_available,
            vault_stats, write_diary, zettel_similarity,
        )
        from app.database import delete_task, get_active_tasks, mark_task_done, save_task

        no_vault_ops = {"list_tasks", "cleanup", "stats"}
        if op not in no_vault_ops and not vault_available():
            return "vault недоступен — проверь OBSIDIAN_VAULT_PATH"

        # ── add_task ──────────────────────────────────────────────────────────
        if op == "add_task":
            if not text:
                return "ошибка: текст задачи пустой"
            if quadrant not in QUADRANTS:
                quadrant = "q2"
            if deadline:
                task_text = add_task_with_deadline(text, quadrant, deadline)
            else:
                add_tasks([text], quadrant=quadrant)
                task_text = text
            if user_id:
                _prio = {"q1": 1, "q2": 2, "q3": 3, "q4": 3}
                save_task(user_id, task_text, _prio.get(quadrant, 2), "")
            q = QUADRANTS[quadrant]
            logger.info("vault add_task [%s]: '%s'", quadrant, task_text[:50])
            return f"добавлено в {q['emoji']} {q['title']}"

        # ── done_task ─────────────────────────────────────────────────────────
        elif op == "done_task":
            if not text:
                return "ошибка: текст задачи пустой"
            found = mark_task_done_obsidian(text)
            if user_id:
                for t in get_active_tasks(user_id):
                    if text.lower() in t[1].lower() or t[1].lower() in text.lower():
                        mark_task_done(t[0], user_id)
                        break
            logger.info("vault done_task: '%s' found=%s", text[:50], found)
            return f"выполнено ✅" if found else f"не нашла задачу «{text}» — проверь список"

        # ── delete_task ───────────────────────────────────────────────────────
        elif op == "delete_task":
            if not text:
                return "ошибка: текст задачи пустой"
            found = delete_task_obsidian(text)
            if user_id:
                for t in get_active_tasks(user_id):
                    if text.lower() in t[1].lower() or t[1].lower() in text.lower():
                        delete_task(t[0], user_id)
                        break
            logger.info("vault delete_task: '%s' found=%s", text[:50], found)
            return f"удалено 🗑️" if found else f"не нашла задачу «{text}»"

        # ── list_tasks ────────────────────────────────────────────────────────
        elif op == "list_tasks":
            return format_all_tasks()

        # ── clear_done ────────────────────────────────────────────────────────
        elif op == "clear_done":
            count = clear_done_tasks()
            # Синхронизируем БД
            if user_id:
                from app.integrations.obsidian import sync_tasks_to_db
                sync_tasks_to_db(user_id)
            return f"удалено {count} выполненных задач" if count else "выполненных задач нет"

        # ── write_diary ───────────────────────────────────────────────────────
        elif op == "write_diary":
            if not text:
                return "ошибка: текст записи пустой"
            path = write_diary(text)
            logger.info("vault write_diary: %s", path)
            return f"записано в дневник"

        # ── add_zettel ────────────────────────────────────────────────────────
        elif op == "add_zettel":
            if not text:
                return "ошибка: текст пустой"
            # Быстрый dedup по сходству без LLM
            titles = list_zettel_titles()
            best_match = None
            best_score = 0.0
            for entry in titles:
                score = zettel_similarity(text, entry)
                if score > best_score:
                    best_score = score
                    best_match = entry
            if best_match and best_score >= 0.45:
                path = update_zettel(best_match["id"], text)
                logger.info("vault update_zettel [score=%.2f]: %s", best_score, best_match["id"])
                return f"дополнила карточку «{best_match['title']}»"
            # Новая карточка
            title = text.split("\n")[0][:60].strip()
            path  = add_zettel(title, text)
            logger.info("vault add_zettel: '%s'", title[:40])
            return f"добавила в базу знаний: «{title}»"

        # ── create_note ───────────────────────────────────────────────────────
        elif op == "create_note":
            if not text:
                return "ошибка: текст пустой"
            title = text.split("\n")[0][:50]
            path  = create_note(title, text)
            logger.info("vault create_note: %s", path)
            return f"заметка создана: «{title}»"

        # ── create_plan ───────────────────────────────────────────────────────
        elif op == "create_plan":
            if not text:
                return "ошибка: текст пустой"
            if plan_horizon not in ("short", "long"):
                plan_horizon = "short"
            title  = text.split("\n")[0][:50]
            path   = create_plan(title, text, plan_horizon)
            folder_rus = "Краткосрочные" if plan_horizon == "short" else "Долгосрочные"
            logger.info("vault create_plan [%s]: %s", plan_horizon, path)
            return f"план в {folder_rus}: «{title}»"

        # ── read_note ─────────────────────────────────────────────────────────
        elif op == "read_note":
            if not text:
                return "ошибка: запрос пустой"
            content = read_note(text)
            if not content:
                return f"заметка «{text}» не найдена"
            # Убираем frontmatter для читаемости
            content = re.sub(r"^---.*?---\n\n?", "", content, flags=re.DOTALL)
            return content[:2000] + ("\n\n...(обрезано)" if len(content) > 2000 else "")

        # ── search ────────────────────────────────────────────────────────────
        elif op == "search":
            if not text:
                return "ошибка: запрос пустой"
            results = search_vault(text)
            if not results:
                return f"ничего не найдено по «{text}»"
            lines = [f"найдено {len(results)} по «{text}»:"]
            for r in results[:5]:
                lines.append(f"• {r['title']}: {r['snippet'][:100]}")
            return "\n".join(lines)

        # ── list_files ────────────────────────────────────────────────────────
        elif op == "list_files":
            folder = folder or "Заметки"
            files  = list_files(folder)
            if not files:
                return f"в «{folder}» пусто"
            names = [f.split("/")[-1].replace(".md", "") for f in files[:20]]
            result = f"📁 {folder} ({len(files)}):\n" + "\n".join(f"• {n}" for n in names)
            if len(files) > 20:
                result += f"\n...и ещё {len(files)-20}"
            return result

        # ── stats ─────────────────────────────────────────────────────────────
        elif op == "stats":
            stats = vault_stats()
            total = sum(stats.values())
            icons = {"Дневник": "📔", "Заметки": "📝", "Zettelkasten": "🧠", "Планы": "📅"}
            lines = ["📊 Vault:"]
            for k, v in stats.items():
                icon = icons.get(k, "")
                lines.append(f"{icon} {k}: {v}".strip())
            lines.append(f"Всего: {total}")
            return "\n".join(lines)

        # ── move_task ─────────────────────────────────────────────────────────
        elif op == "move_task":
            if not text:
                return "ошибка: текст задачи пустой"
            tq = target_quadrant if target_quadrant in QUADRANTS else "q2"
            ok = move_task(text, tq)
            q  = QUADRANTS[tq]
            return f"перемещено в {q['emoji']} {q['title']}" if ok else f"задача не найдена: «{text}»"

        # ── overdue_tasks ─────────────────────────────────────────────────────
        elif op == "overdue_tasks":
            tasks = get_overdue_tasks(days_threshold=1)  # сегодня + завтра
            if not tasks:
                return "нет задач с дедлайном на сегодня/завтра"
            lines = ["⏰ Задачи с дедлайном:"]
            for t in tasks:
                flag = " 🔴 ПРОСРОЧЕНО" if t["overdue"] else ""
                lines.append(f"  • {t['text']}{flag}")
            return "\n".join(lines)

        # ── undo_task ─────────────────────────────────────────────────────────
        elif op == "undo_task":
            if not text:
                return "ошибка: текст задачи пустой"
            ok = undo_task(text)
            if user_id and ok:
                for t in get_active_tasks(user_id):
                    if text.lower() in t[1].lower():
                        mark_task_done(t[0], user_id)  # снимаем done в БД
                        break
            logger.info("vault undo_task: '%s' ok=%s", text[:50], ok)
            return f"возвращено в активные: «{text}»" if ok else f"не нашла: «{text}»"

        # ── add_event ─────────────────────────────────────────────────────────
        elif op == "add_event":
            if not text:
                return "ошибка: название события пустое"
            from datetime import date as _date
            date_str = event_date or str(_date.today())
            from app.calendar_service import create_event
            ev = create_event(
                user_id=user_id, date=date_str, title=text,
                time_start=event_time, time_end=event_time_end,
                description=deadline, color=event_color,
            )
            t = f"{ev['time_start']}–{ev['time_end']}" if ev["time_start"] else "весь день"
            return f"событие добавлено: {ev['title']} · {date_str} · {t}"

        # ── get_events ────────────────────────────────────────────────────────
        elif op == "get_events":
            from datetime import date as _date
            from app.calendar_service import format_day_for_telegram, format_upcoming_for_telegram
            if event_date:
                return format_day_for_telegram(user_id, event_date)
            return format_upcoming_for_telegram(user_id)

        # ── delete_event ──────────────────────────────────────────────────────
        elif op == "delete_event":
            if not event_id:
                return "ошибка: не указан id события"
            from app.database import delete_event as _del
            ok = _del(event_id, user_id)
            return "событие удалено" if ok else "событие не найдено"

        # ── week_plan ─────────────────────────────────────────────────────────
        elif op == "week_plan":
            return get_week_plan()

        # ── batch_done ────────────────────────────────────────────────────────
        elif op == "batch_done":
            if not text:
                return "ошибка: список задач пустой"
            # text = задачи через ; или новую строку
            import re as _re
            tasks = [t.strip() for t in _re.split(r"[;\n]", text) if t.strip()]
            result = mark_multiple_done(tasks)
            parts = []
            if result["done"]:
                parts.append(f"выполнено {len(result['done'])}: " + ", ".join(f"«{t}»" for t in result["done"]))
            if result["not_found"]:
                parts.append(f"не найдено: " + ", ".join(f"«{t}»" for t in result["not_found"]))
            return "; ".join(parts) if parts else "задачи не найдены"

        # ── diary_context ─────────────────────────────────────────────────────
        elif op == "diary_context":
            ctx_text = get_diary_context(days=3)
            return ctx_text if ctx_text else "дневник пустой за последние 3 дня"

        # ── add_event ─────────────────────────────────────────────────────────
        elif op == "add_event":
            if not text or not event_date:
                return "ошибка: нужны text (название) и event_date (YYYY-MM-DD)"
            from app.database import save_event
            eid = save_event(
                user_id=user_id, date=event_date, title=text,
                time_start=event_time_start, time_end=event_time_end,
                description="", color=event_color or "blue",
            )
            time_str = f" в {event_time_start}" if event_time_start else ""
            logger.info("vault add_event: %s %s", event_date, text[:40])
            return f"событие добавлено: {event_date}{time_str} — «{text}» (id={eid})"

        # ── list_events ────────────────────────────────────────────────────────
        elif op == "list_events":
            from app.database import get_events_for_date, get_upcoming_events
            if event_date:
                events = get_events_for_date(user_id, event_date)
                if not events:
                    return f"событий на {event_date} нет"
                lines = [f"📅 {event_date}:"]
                for e in events:
                    t = f"{e['time_start']}" + (f"–{e['time_end']}" if e['time_end'] else "")
                    lines.append(f"  • {t + ' ' if t else ''}{e['title']}")
                return "\n".join(lines)
            else:
                events = get_upcoming_events(user_id, limit=7)
                if not events:
                    return "ближайших событий нет"
                lines = ["📅 Ближайшие события:"]
                prev_date = ""
                for e in events:
                    if e["date"] != prev_date:
                        lines.append(f"\n{e['date']}:")
                        prev_date = e["date"]
                    t = e["time_start"] + (f"–{e['time_end']}" if e["time_end"] else "")
                    lines.append(f"  • {t + ' ' if t else ''}{e['title']}")
                return "\n".join(lines)

        # ── delete_event ───────────────────────────────────────────────────────
        elif op == "delete_event":
            from app.database import delete_event as _del_ev, get_events_for_date, get_upcoming_events
            # Пробуем удалить по id (если text — число)
            if text.isdigit():
                ok = _del_ev(int(text), user_id)
                return f"событие удалено" if ok else f"событие id={text} не найдено"
            # Ищем по названию среди ближайших
            events = get_upcoming_events(user_id, limit=30)
            for e in events:
                if text.lower() in e["title"].lower():
                    ok = _del_ev(e["id"], user_id)
                    return f"удалено: «{e['title']}» ({e['date']})"
            return f"не нашла событие «{text}»"

        # ── cleanup ───────────────────────────────────────────────────────────
        elif op == "cleanup":
            result  = cleanup_vault()
            deleted = result.get("deleted", [])
            return f"удалено {len(deleted)}: {', '.join(deleted)}" if deleted else "vault чист"

        else:
            return f"неизвестная операция: {op}"

    except Exception as e:
        logger.exception("vault_tool op=%s", op)
        return f"ошибка: {e}"


async def process_tool_calls(tool_calls: list, user_id: int) -> list[dict]:
    """Параллельно обрабатывает все tool_calls."""
    import asyncio

    async def _one(tc: dict) -> dict:
        if tc.get("name") != "vault":
            return {"tool_call_id": tc.get("id", ""), "result": "unknown tool"}
        args   = tc.get("args", {})
        result = await run_vault_op(
            op=args.get("op", ""),
            text=args.get("text", ""),
            quadrant=args.get("quadrant", "q2"),
            plan_horizon=args.get("plan_horizon", "short"),
            folder=args.get("folder", "Заметки"),
            deadline=args.get("deadline", ""),
            target_quadrant=args.get("target_quadrant", "q2"),
            event_date=args.get("event_date", ""),
            event_time=args.get("event_time", ""),
            event_time_end=args.get("event_time_end", ""),
            event_color=args.get("event_color", "blue"),
            event_id=int(args.get("event_id", 0)),
            user_id=user_id,
        )
        return {"tool_call_id": tc.get("id", "call_0"), "result": result}

    return list(await asyncio.gather(*[_one(tc) for tc in tool_calls]))
