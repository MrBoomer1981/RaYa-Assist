"""
calendar_agent.py — управление событиями календаря.

Умеет:
- Добавлять события с датой, временем, описанием
- Показывать события на день / неделю / ближайшие
- Удалять события по ID или названию
- Обновлять время/название существующих событий

LLM извлекает параметры → calendar_service выполняет операцию → ответ пользователю.
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
from app.database import delete_event, get_upcoming_events, update_event
from app.utils import strip_json
from app.config import settings as _cfg

logger = logging.getLogger(__name__)

_SYSTEM = """\
Ты RaYa — личный ассистент. Управляешь календарём пользователя.

Операции — возвращай XML-теги в ответе:

1. Добавить событие:
<add_event>{"date":"ГГГГ-ММ-ДД","title":"название","time_start":"ЧЧ:ММ","time_end":"ЧЧ:ММ","description":"доп инфо","color":"blue"}</add_event>
  color: blue (дефолт) | green | red | orange | purple

2. Показать события на дату:
<show_day>ГГГГ-ММ-ДД</show_day>

3. Показать ближайшие события:
<show_upcoming/>

4. Удалить событие по ID:
<delete_event>ID</delete_event>

5. Обновить событие:
<update_event>{"id":ID,"title":"новое","time_start":"ЧЧ:ММ","time_end":"ЧЧ:ММ"}</update_event>

Правила разбора дат:
- "сегодня" → текущая дата
- "завтра" → завтрашняя дата
- "послезавтра" → через 2 дня
- "в понедельник", "в пятницу" → ближайший такой день
- "15 мая", "15.05" → конкретная дата текущего/следующего года
- Если дата не указана явно — уточни одним вопросом

Правила времени:
- "в 14:00", "в два часа", "в 14" → time_start
- "с 10 до 12", "с 10:00 до 12:00" → time_start + time_end
- Если время не указано → оставь пустым (весь день)

Отвечай живо и коротко. Подтверди что сделал. Обращайся по имени.\
"""

# ── Парсинг "человеческих" дат ────────────────────────────────────────────────

_DAYS_RU = {
    "понедельник": 0, "вторник": 1, "среда": 2, "среду": 2,
    "четверг": 3, "пятница": 4, "пятницу": 4,
    "суббота": 5, "субботу": 5, "воскресенье": 6,
}


def _resolve_date(raw: str) -> str:
    """Преобразует текстовую дату в ГГГГ-ММ-ДД. Возвращает '' если не распознал."""
    raw = raw.strip().lower()
    today = datetime.utcnow().date()

    if raw in ("сегодня", "today"):
        return today.isoformat()
    if raw in ("завтра", "tomorrow"):
        return (today + timedelta(days=1)).isoformat()
    if raw in ("послезавтра",):
        return (today + timedelta(days=2)).isoformat()

    for day_name, weekday in _DAYS_RU.items():
        if day_name in raw:
            delta = (weekday - today.weekday()) % 7 or 7
            return (today + timedelta(days=delta)).isoformat()

    # "15 мая" / "15.05" / "2026-05-15"
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d.%m"):
        try:
            dt = datetime.strptime(raw, fmt)
            if fmt == "%d.%m":
                dt = dt.replace(year=today.year)
                if dt.date() < today:
                    dt = dt.replace(year=today.year + 1)
            return dt.date().isoformat()
        except ValueError:
            continue

    return ""


async def _sync_day_to_obsidian(user_id: int, date: str) -> None:
    """Синхронизирует события одного дня в Obsidian. Fire-and-forget."""
    try:
        from app.services.obsidian import save_calendar_day as obs_cal
        from app.database import get_upcoming_events
        import sqlite3

        # Берём события именно на эту дату из БД
        from app.database import _conn
        with _conn() as con:
            rows = con.execute(
                """SELECT id, date, time_start, time_end, title, description, color
                   FROM events WHERE user_id = ? AND date = ?
                   ORDER BY CASE WHEN time_start IS NULL THEN '99:99' ELSE time_start END""",
                (user_id, date)
            ).fetchall()
        events = [{"id": r[0], "date": r[1], "time_start": r[2] or "",
                   "time_end": r[3] or "", "title": r[4],
                   "description": r[5], "color": r[6]} for r in rows]
        await obs_cal(date, events)
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning("📅 Obsidian calendar sync failed: %s", e)


class CalendarAgent(BaseAgent):
    agent_name = "calendar"
    timeout    = 30

    def _system_prompt(self) -> str:
        return _SYSTEM

    async def _execute(self, ctx: AgentContext) -> AgentResult:
        now   = datetime.utcnow()
        today = now.strftime("%Y-%m-%d")

        # Быстрый показ без LLM
        msg = ctx.message.lower()
        if any(kw in msg for kw in ("покажи события", "что сегодня", "мой календарь",
                                     "ближайшие события", "что запланировано")):
            upcoming = format_upcoming_for_telegram(ctx.user_id)
            return AgentResult(success=True, content=upcoming,
                               agent_name=self.agent_name, needs_critic=False)

        if "сегодня" in msg and any(kw in msg for kw in ("события", "расписание", "план")):
            day_text = format_day_for_telegram(ctx.user_id, today)
            return AgentResult(success=True, content=day_text,
                               agent_name=self.agent_name, needs_critic=False)

        # Контекст существующих событий для LLM
        calendar_ctx = self._build_calendar_context(ctx.user_id, today)

        messages = [
            SystemMessage(content=_SYSTEM + f"\n\nСегодня: {today} (UTC)"),
            *ctx.history,
            HumanMessage(content=ctx.message + calendar_ctx),
        ]
        response = await self._llm.ainvoke(messages)
        raw = str(response.content)

        reply = raw

        # ── ADD EVENT ──────────────────────────────────────────────────────────
        add_match = re.search(r"<add_event>(.*?)</add_event>", raw, re.DOTALL)
        if add_match:
            try:
                data  = json.loads(strip_json(add_match.group(1)))
                date  = _resolve_date(data.get("date", "")) or today
                event = create_event(
                    user_id=ctx.user_id,
                    date=date,
                    title=data.get("title", "Событие"),
                    time_start=data.get("time_start", ""),
                    time_end=data.get("time_end", ""),
                    description=data.get("description", ""),
                    color=data.get("color", "blue"),
                )
                logger.info("📅 CalendarAgent: добавлено '%s' на %s | user_id=%s",
                            event["title"][:40], date, ctx.user_id)
                if _cfg.obsidian_enabled:
                    await _sync_day_to_obsidian(ctx.user_id, date)
            except Exception as e:
                logger.warning("calendar: ошибка добавления события: %s", e)

        # ── SHOW DAY ───────────────────────────────────────────────────────────
        show_match = re.search(r"<show_day>(.*?)</show_day>", raw, re.DOTALL)
        if show_match:
            date = _resolve_date(show_match.group(1).strip()) or today
            reply = format_day_for_telegram(ctx.user_id, date)
            # Убираем тег из ответа
            reply = re.sub(r"<show_day>.*?</show_day>", "", raw, flags=re.DOTALL).strip()
            if not reply:
                reply = format_day_for_telegram(ctx.user_id, date)
            else:
                reply += "\n\n" + format_day_for_telegram(ctx.user_id, date)

        # ── SHOW UPCOMING ──────────────────────────────────────────────────────
        if "<show_upcoming" in raw:
            upcoming = format_upcoming_for_telegram(ctx.user_id)
            reply = re.sub(r"<show_upcoming\s*/>", "", raw).strip()
            reply = (reply + "\n\n" + upcoming) if reply else upcoming

        # ── DELETE EVENT ───────────────────────────────────────────────────────
        for del_match in re.finditer(r"<delete_event>(\d+)</delete_event>", raw):
            event_id = int(del_match.group(1))
            ok = delete_event(event_id, ctx.user_id)
            logger.info("🗑️ CalendarAgent: удалено событие #%d ok=%s | user_id=%s",
                        event_id, ok, ctx.user_id)
            if ok and _cfg.obsidian_enabled:
                # Дата нужна для sync — берём из контекста (уже распаршена выше)
                _sync_date = show_match.group(1).strip() if show_match else today
                await _sync_day_to_obsidian(ctx.user_id, _resolve_date(_sync_date) or today)

        # ── UPDATE EVENT ───────────────────────────────────────────────────────
        upd_match = re.search(r"<update_event>(.*?)</update_event>", raw, re.DOTALL)
        if upd_match:
            try:
                data     = json.loads(strip_json(upd_match.group(1)))
                event_id = int(data.pop("id"))
                ok = update_event(event_id, ctx.user_id, **data)
                logger.info("✏️ CalendarAgent: обновлено #%d ok=%s | user_id=%s",
                            event_id, ok, ctx.user_id)
                if ok and _cfg.obsidian_enabled:
                    upd_date = data.get("date", today)
                    await _sync_day_to_obsidian(ctx.user_id, upd_date)
            except Exception as e:
                logger.warning("calendar: ошибка обновления: %s", e)

        # Чистим все теги из финального ответа
        reply = re.sub(
            r"<(add_event|delete_event|update_event|show_day|show_upcoming)[^>]*>.*?</(add_event|delete_event|update_event|show_day)>",
            "", reply, flags=re.DOTALL,
        )
        reply = re.sub(r"<show_upcoming\s*/>", "", reply)
        reply = reply.strip()

        return AgentResult(
            success=True,
            content=reply or "Готово.",
            agent_name=self.agent_name,
            needs_critic=False,
        )

    def _build_calendar_context(self, user_id: int, today: str) -> str:
        """Добавляет ближайшие события в контекст для LLM."""
        events = get_upcoming_events(user_id, limit=10)
        if not events:
            return "\n\n[Календарь пуст]"
        lines = ["\n\n[Ближайшие события в календаре (для справки):"]
        for ev in events:
            t = f" {ev['time_start']}" if ev["time_start"] else ""
            lines.append(f"  ID={ev['id']} | {ev['date']}{t} — {ev['title']}")
        lines.append("]")
        return "\n".join(lines)
