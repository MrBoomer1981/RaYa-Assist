"""
calendar_service.py — сервис событий для RaYa.

Хранение: SQLite (таблица events) — единственный источник правды.
          (односторонняя запись, без синхронизации обратно).

  ## 📅 События
  - 09:00–10:00 🔵 Встреча с командой
  - Весь день 🟢 День рождения Мамы
"""
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path

from app.database import (
    delete_event, get_events_for_date, get_events_for_month,
    get_upcoming_events, save_event, update_event,
)

logger = logging.getLogger(__name__)

_COLOR_EMOJI = {
    "blue":   "🔵",
    "green":  "🟢",
    "red":    "🔴",
    "orange": "🟠",
    "purple": "🟣",
}

_MONTHS_RU = [
    "", "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
    "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь",
]

_DAYS_RU = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


# ── Создание события ──────────────────────────────────────────────────────────

def create_event(user_id: int, date: str, title: str,
                 time_start: str = "", time_end: str = "",
                 description: str = "", color: str = "blue") -> dict:
    """
    Возвращает dict созданного события.
    """
    event_id = save_event(
        user_id=user_id, date=date, title=title,
        time_start=time_start, time_end=time_end,
        description=description, color=color,
    )


    logger.info("📅 Событие создано: %s %s '%s'", date, time_start, title[:40])
    return {
        "id": event_id, "date": date, "title": title,
        "time_start": time_start, "time_end": time_end,
        "description": description, "color": color,
    }




def get_day(user_id: int, date: str) -> dict:
    events   = get_events_for_date(user_id, date)
    notes    = ""
    return {"date": date, "events": events, "notes": notes}


def get_month(user_id: int, year: int, month: int) -> dict:
    """
    Возвращает данные месяца для календарного вида.
    Группирует события по дням для быстрого рендера.
    """
    events      = get_events_for_month(user_id, year, month)
    by_day: dict[str, list] = {}
    for ev in events:
        by_day.setdefault(ev["date"], []).append(ev)

    return {
        "year": year, "month": month,
        "month_name": _MONTHS_RU[month],
        "by_day": by_day,
        "events": events,
    }



def format_day_for_telegram(user_id: int, date: str) -> str:
    """Форматирует день для ответа в Telegram."""
    try:
        y, m, d = date.split("-")
        dt      = datetime(int(y), int(m), int(d))
        day_ru  = _DAYS_RU[dt.weekday()]
        date_ru = f"{d} {_MONTHS_RU[int(m)]}"
    except Exception:
        day_ru  = ""
        date_ru = date

    events = get_events_for_date(user_id, date)
    lines  = [f"**{day_ru}, {date_ru}**\n"]

    if not events:
        lines.append("_Событий нет_")
    else:
        for ev in events:
            emoji    = _COLOR_EMOJI.get(ev["color"], "🔵")
            if ev["time_start"] and ev["time_end"]:
                t = f"{ev['time_start']}–{ev['time_end']}"
            elif ev["time_start"]:
                t = ev["time_start"]
            else:
                t = "Весь день"
            lines.append(f"{emoji} {t} — {ev['title']}")
            if ev["description"]:
                lines.append(f"   _{ev['description']}_")

    return "\n".join(lines)


def format_upcoming_for_telegram(user_id: int) -> str:
    """Ближайшие события для контекста."""
    events = get_upcoming_events(user_id, limit=5)
    if not events:
        return "Ближайших событий нет"
    lines = ["**Ближайшие события:**\n"]
    for ev in events:
        emoji = _COLOR_EMOJI.get(ev["color"], "🔵")
        t     = ev["time_start"] or "Весь день"
        lines.append(f"{emoji} {ev['date']} {t} — {ev['title']}")
    return "\n".join(lines)
