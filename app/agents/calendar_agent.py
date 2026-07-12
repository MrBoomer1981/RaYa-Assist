"""
calendar_agent.py — расширенный календарь RaYa.

Умеет:
- Добавлять события с датой, временем, описанием, важностью
- Помечать события как важные ⭐ или критичные 🔥
- Повторяющиеся события (дни рождения, еженедельные встречи)
- Напоминания за N дней до события
- Показывать день / неделю / ближайшие
- Искать события по названию
- Удалять и обновлять события
"""
import json
import logging
import re
from datetime import datetime, timedelta

from langchain_core.messages import HumanMessage, SystemMessage

from app.agents.base_agent import AgentContext, AgentResult, BaseAgent
from app.calendar_service import (
    create_event, format_day_for_telegram, format_upcoming_for_telegram,
)
from app.database import delete_event, update_event, _conn
from app.utils import strip_json, utcnow

logger = logging.getLogger(__name__)

_SYSTEM = """\
Ты RaYa — личный ассистент. Управляешь календарём пользователя.
Сегодня: {today}

Операции — возвращай XML-теги:

1. Добавить событие:
<add_event>{"date":"ГГГГ-ММ-ДД","title":"название","time_start":"ЧЧ:ММ","time_end":"ЧЧ:ММ",
"description":"","color":"blue","importance":0,"repeat":"","remind_days":0}</add_event>
  color: blue|green|red|orange|purple
  importance: 0=обычное, 1=важное ⭐, 2=критично 🔥
  repeat: "" | "yearly" | "monthly" | "weekly"
  remind_days: 0=нет, 1=за день, 3=за 3 дня, 7=за неделю

2. Показать день: <show_day>ГГГГ-ММ-ДД</show_day>
3. Показать неделю: <show_week>ГГГГ-ММ-ДД</show_week>
4. Ближайшие: <show_upcoming/>
5. Поиск: <search_events>запрос</search_events>
6. Удалить: <delete_event>ID</delete_event>
7. Обновить: <update_event>{"id":ID,"title":"","time_start":"","importance":1}</update_event>
8. Отметить важным: <mark_important id="ID" level="1"/>  (level: 1=важное, 2=критично)

ПРАВИЛА ДАТ:
- "сегодня/завтра/послезавтра" → считай от {today}
- "в пятницу/понедельник" → ближайший такой день
- "15 мая", "15.05", "15/05" → дата текущего/следующего года
- "14 мая 2027" → конкретный год
- День рождения / ежегодное → repeat="yearly", importance=1

ПРАВИЛА ВАЖНОСТИ:
- "важное", "не забыть", "обязательно" → importance=1
- "критично", "срочно", "дедлайн" → importance=2
- Дни рождения близких → importance=1, repeat="yearly"
- remind_days: для важных ставь 1-3 дня, для критичных 7 дней

Отвечай кратко. Подтверди что добавил/изменил. Обращайся по имени.\
"""

# ── Парсинг дат ───────────────────────────────────────────────────────────────

_DAYS_RU = {
    "понедельник": 0, "пн": 0,
    "вторник": 1,    "вт": 1,
    "среда": 2, "среду": 2, "ср": 2,
    "четверг": 3,    "чт": 3,
    "пятница": 4, "пятницу": 4, "пт": 4,
    "суббота": 5, "субботу": 5, "сб": 5,
    "воскресенье": 6, "вс": 6,
}

_MONTHS_RU = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4,
    "мая": 5, "июня": 6, "июля": 7, "августа": 8,
    "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
    "январь": 1, "февраль": 2, "март": 3, "апрель": 4,
    "май": 5, "июнь": 6, "июль": 7, "август": 8,
    "сентябрь": 9, "октябрь": 10, "ноябрь": 11, "декабрь": 12,
}


def _resolve_date(raw: str) -> str:
    """Преобразует текстовую дату в ГГГГ-ММ-ДД. '' если не распознал."""
    if not raw:
        return ""
    raw_orig = raw.strip()
    raw = raw_orig.lower().strip()
    today = utcnow().date()

    # Относительные
    if raw in ("сегодня", "today"):
        return today.isoformat()
    if raw in ("завтра", "tomorrow"):
        return (today + timedelta(days=1)).isoformat()
    if raw in ("послезавтра",):
        return (today + timedelta(days=2)).isoformat()
    if "через" in raw:
        m = re.search(r"через\s+(\d+)\s*(день|дня|дней|неделю|недели|недель|месяц)", raw)
        if m:
            n, unit = int(m.group(1)), m.group(2)
            if "нед" in unit:
                return (today + timedelta(weeks=n)).isoformat()
            if "мес" in unit:
                return (today + timedelta(days=30*n)).isoformat()
            return (today + timedelta(days=n)).isoformat()

    # День недели
    for day_name, weekday in _DAYS_RU.items():
        if day_name in raw:
            delta = (weekday - today.weekday()) % 7 or 7
            return (today + timedelta(days=delta)).isoformat()

    # "15 мая 2027" / "15 мая"
    m = re.search(r"(\d{1,2})\s+([а-яё]+)(?:\s+(\d{4}))?", raw)
    if m:
        day = int(m.group(1))
        month_str = m.group(2)
        year = int(m.group(3)) if m.group(3) else None
        month = _MONTHS_RU.get(month_str)
        if month:
            if year is None:
                year = today.year
                try:
                    from datetime import date
                    d = date(year, month, day)
                    if d < today:
                        year += 1
                except ValueError:
                    pass
            try:
                return datetime(year, month, day).date().isoformat()
            except ValueError:
                pass

    # ISO и числовые форматы
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%d.%m"):
        try:
            dt = datetime.strptime(raw_orig, fmt)
            if fmt == "%d.%m":
                dt = dt.replace(year=today.year)
                if dt.date() < today:
                    dt = dt.replace(year=today.year + 1)
            return dt.date().isoformat()
        except ValueError:
            continue

    return ""


# ── Вспомогательные ───────────────────────────────────────────────────────────

def _search_events(user_id: int, query: str) -> str:
    """Полнотекстовый поиск по событиям."""
    try:
        with _conn() as con:
            rows = con.execute(
                """SELECT id, date, time_start, title, importance, repeat
                   FROM events
                   WHERE user_id=? AND (
                       lower(title) LIKE ? OR lower(description) LIKE ?
                   )
                   ORDER BY date ASC LIMIT 10""",
                (user_id, f"%{query.lower()}%", f"%{query.lower()}%"),
            ).fetchall()

        if not rows:
            return f"🔍 По запросу «{query}» событий не найдено."

        _COLOR_EMOJI = {"blue":"🔵","green":"🟢","red":"🔴","orange":"🟠","purple":"🟣"}
        _IMP = {0:"", 1:" ⭐", 2:" 🔥"}
        lines = [f"🔍 Найдено {len(rows)} событий по «{query}»:\n"]
        for r in rows:
            imp = _IMP.get(r[4] or 0, "")
            rep = " 🔁" if r[5] else ""
            t = f" {r[2]}" if r[2] else ""
            lines.append(f"[{r[0]}] {r[1]}{t} — {r[3]}{imp}{rep}")
        return "\n".join(lines)
    except Exception as e:
        return f"⚠️ Ошибка поиска: {e}"


def _format_week(user_id: int, start_date_str: str) -> str:
    """Форматирует неделю начиная с даты."""
    try:
        start = datetime.strptime(start_date_str, "%Y-%m-%d").date()
    except ValueError:
        start = utcnow().date()

    _DAYS_NAMES = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    lines = [f"**Неделя с {start.strftime('%d.%m')}**\n"]

    with _conn() as con:
        for i in range(7):
            day = start + timedelta(days=i)
            day_str = day.isoformat()
            rows = con.execute(
                """SELECT time_start, title, importance, color
                   FROM events WHERE user_id=? AND date=?
                   ORDER BY CASE WHEN time_start IS NULL OR time_start='' THEN '99:99' ELSE time_start END""",
                (user_id, day_str),
            ).fetchall()

            day_name = _DAYS_NAMES[day.weekday()]
            header = f"\n**{day_name} {day.strftime('%d.%m')}**"
            if not rows:
                lines.append(f"{header} — _свободно_")
            else:
                lines.append(header)
                for r in rows:
                    _COLOR_EMOJI = {"blue":"🔵","green":"🟢","red":"🔴","orange":"🟠","purple":"🟣"}
                    _IMP = {0:"", 1:" ⭐", 2:" 🔥"}
                    emoji = _COLOR_EMOJI.get(r[3] or "blue", "🔵")
                    t = r[0] or "Весь день"
                    imp = _IMP.get(r[2] or 0, "")
                    lines.append(f"  {emoji} {t} — {r[1]}{imp}")
    return "\n".join(lines)


# ── Агент ─────────────────────────────────────────────────────────────────────

class CalendarAgent(BaseAgent):
    agent_name = "calendar"
    timeout    = 30

    def _system_prompt(self) -> str:
        today = utcnow().strftime("%Y-%m-%d")
        return _SYSTEM.replace("{today}", today)

    async def _execute(self, ctx: AgentContext) -> AgentResult:
        today = utcnow().strftime("%Y-%m-%d")

        messages = [
            SystemMessage(content=self._system_prompt()),
            *ctx.history[-6:],
            HumanMessage(content=ctx.message),
        ]
        response = await self._llm.ainvoke(messages)
        raw = str(response.content)

        # ── ADD EVENT ─────────────────────────────────────────────────────────
        for m in re.finditer(r"<add_event>(.*?)</add_event>", raw, re.DOTALL):
            try:
                data     = json.loads(strip_json(m.group(1)))
                date     = _resolve_date(data.get("date", "")) or today
                title    = data.get("title", "").strip()
                if not title:
                    continue
                event = create_event(
                    user_id     = ctx.user_id,
                    date        = date,
                    title       = title,
                    time_start  = data.get("time_start", ""),
                    time_end    = data.get("time_end", ""),
                    description = data.get("description", ""),
                    color       = data.get("color", "blue"),
                    importance  = int(data.get("importance", 0)),
                    repeat      = data.get("repeat", ""),
                    remind_days = int(data.get("remind_days", 0)),
                )
                logger.info(
                    "📅 CalendarAgent: добавлено '%s' на %s imp=%d | user_id=%s",
                    title[:40], date, event.get("importance",0), ctx.user_id,
                )
            except Exception as e:
                logger.warning("calendar: add_event error: %s", e)

        # ── MARK IMPORTANT ────────────────────────────────────────────────────
        for m in re.finditer(r'<mark_important\s+id="(\d+)"\s+level="(\d+)"/>', raw):
            event_id = int(m.group(1))
            level    = int(m.group(2))
            try:
                with _conn() as con:
                    row = con.execute(
                        "SELECT date FROM events WHERE id=? AND user_id=?",
                        (event_id, ctx.user_id),
                    ).fetchone()
                    if row:
                        con.execute(
                            "UPDATE events SET importance=? WHERE id=? AND user_id=?",
                            (level, event_id, ctx.user_id),
                        )
                        logger.info("⭐ CalendarAgent: importance=%d для #%d", level, event_id)
            except Exception as e:
                logger.warning("calendar: mark_important error: %s", e)

        # ── DELETE EVENT ──────────────────────────────────────────────────────
        for m in re.finditer(r"<delete_event>(\d+)</delete_event>", raw):
            event_id = int(m.group(1))
            try:
                ok = delete_event(event_id, ctx.user_id)
                logger.info("🗑️ CalendarAgent: удалено #%d ok=%s | user_id=%s",
                            event_id, ok, ctx.user_id)
            except Exception as e:
                logger.warning("calendar: delete_event error: %s", e)

        # ── UPDATE EVENT ──────────────────────────────────────────────────────
        for m in re.finditer(r"<update_event>(.*?)</update_event>", raw, re.DOTALL):
            try:
                data     = json.loads(strip_json(m.group(1)))
                event_id = int(data.pop("id"))
                # Если меняем дату — разбираем её
                if "date" in data:
                    data["date"] = _resolve_date(data["date"]) or data["date"]
                ok = update_event(event_id, ctx.user_id, **data)
                logger.info("✏️ CalendarAgent: обновлено #%d ok=%s", event_id, ok)
            except Exception as e:
                logger.warning("calendar: update_event error: %s", e)

        # ── ПОКАЗ ─────────────────────────────────────────────────────────────
        reply_parts = []

        show_day_match = re.search(r"<show_day>(.*?)</show_day>", raw, re.DOTALL)
        if show_day_match:
            date = _resolve_date(show_day_match.group(1).strip()) or today
            reply_parts.append(format_day_for_telegram(ctx.user_id, date))

        show_week_match = re.search(r"<show_week>(.*?)</show_week>", raw, re.DOTALL)
        if show_week_match:
            date = _resolve_date(show_week_match.group(1).strip()) or today
            reply_parts.append(_format_week(ctx.user_id, date))

        if re.search(r"<show_upcoming\s*/>", raw):
            reply_parts.append(format_upcoming_for_telegram(ctx.user_id))

        search_match = re.search(r"<search_events>(.*?)</search_events>", raw, re.DOTALL)
        if search_match:
            reply_parts.append(_search_events(ctx.user_id, search_match.group(1).strip()))

        # Убираем XML-теги из ответа LLM
        reply = re.sub(
            r"<(add_event|show_day|show_week|show_upcoming|delete_event|update_event|"
            r"mark_important|search_events)[^>]*>.*?</(add_event|show_day|show_week|"
            r"delete_event|update_event|search_events)>",
            "", raw, flags=re.DOTALL,
        )
        reply = re.sub(r"<(show_upcoming|mark_important)[^/]*/?>", "", reply)
        reply = reply.strip()

        if reply_parts:
            reply = (reply + "\n\n" + "\n\n".join(reply_parts)).strip()

        return AgentResult(
            success=True,
            content=reply or "Готово.",
            agent_name=self.agent_name,
            needs_critic=False,
        )
