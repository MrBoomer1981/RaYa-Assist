"""
morning_agent.py — утренний дайджест RaYa.

Состав:
  🌤 Погода         — wttr.in с городом из Core Memory
  📅 Расписание     — события из calendar_service / фото-расписания
  ✅ Задачи         — активные задачи с дедлайнами
  🤖 AI-новости     — последние 24ч, акцент на AI/LLM/OpenAI/Anthropic/Google
  💻 IT-новости     — технологии, стартапы, продукты, hardware
  🌍 Мир            — важнейшие события дня
  💬 Философия      — цитата дня от мыслителей

Каждый блок независим — если упал, остальные показываются.
LLM вызывается ОДИН раз для новостей (экономим токены).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from langchain_core.messages import SystemMessage, HumanMessage
from langchain_groq import ChatGroq

from app.agents.base_agent import AgentContext, AgentResult, BaseAgent
from app.config import settings
from app.utils import utcnow

logger = logging.getLogger(__name__)

# ── Конфиг ────────────────────────────────────────────────────────────────────

_MOSCOW_UTC_OFFSET = 3
_WEATHER_API       = "https://wttr.in/{city}?format=j1&lang=ru"
_WEATHER_API_ANON  = "https://wttr.in/?format=j1&lang=ru"  # если город неизвестен

# Поисковые запросы для новостного блока
# Ключевые слова для AI — максимально специфичны
_NEWS_QUERIES = {
    "🤖 AI / LLM": [
        "OpenAI GPT latest news today",
        "Anthropic Claude AI news today",
        "Google DeepMind Gemini news today",
        "artificial intelligence LLM research news today",
        "AI startup funding news today",
    ],
    "💻 IT / Tech": [
        "tech industry news today",
        "new software product launch today",
        "cybersecurity breach news today",
        "Apple Microsoft Amazon Google product news today",
    ],
    "🌍 Мир": [
        "breaking world news today",
        "major geopolitical events today",
    ],
}

_QUOTES = [
    ("Сократ", "Я знаю, что ничего не знаю."),
    ("Марк Аврелий", "Потеря — это не что иное, как изменение, а изменение — это радость природы."),
    ("Эпиктет", "Не требуй, чтобы происходящее было так, как ты хочешь; но желай, чтобы происходящее было так, как есть — и ты будешь иметь душевный покой."),
    ("Ницше", "Без музыки жизнь была бы ошибкой."),
    ("Камю", "Нужно представлять Сизифа счастливым."),
    ("Сенека", "Не трать время на то, что другие считают важным. Живи по-своему."),
    ("Витгенштейн", "О чём невозможно говорить, о том следует молчать."),
    ("Гераклит", "Всё течёт, всё изменяется."),
    ("Спиноза", "Мир будет счастлив только тогда, когда у каждого человека будет душа философа."),
    ("Паскаль", "Всё несчастья людей происходят от одной причины: они не умеют спокойно сидеть в своей комнате."),
    ("Декарт", "Я мыслю — следовательно, я существую."),
    ("Аристотель", "Мы есть то, что мы делаем постоянно. Совершенство — это привычка."),
    ("Конфуций", "Когда вам покажется, что цель недостижима, не изменяйте цель — изменяйте план действий."),
    ("Дао Дэ Цзин", "Знающий не говорит. Говорящий не знает."),
    ("Хайдеггер", "Язык — это дом бытия."),
]


# ── Агент ─────────────────────────────────────────────────────────────────────

class MorningAgent(BaseAgent):
    agent_name = "morning"
    timeout    = 90

    def _system_prompt(self) -> str:
        return ""

    async def _execute(self, ctx: AgentContext) -> AgentResult:
        now_msk = datetime.now(timezone.utc) + timedelta(hours=_MOSCOW_UTC_OFFSET)

        # Все блоки параллельно
        (
            weather_res,
            events_res,
            tasks_res,
            news_res,
            photo_res,
        ) = await asyncio.gather(
            _get_weather(ctx.user_id),
            _get_events(ctx.user_id, now_msk),
            _get_tasks(ctx.user_id),
            _get_news_digest(),
            _get_photo_schedule(ctx.user_id),
            return_exceptions=True,
        )

        def safe(v, default=""):
            return v if isinstance(v, str) else default

        weather     = safe(weather_res)
        events      = safe(events_res)
        tasks       = safe(tasks_res)
        news        = safe(news_res)
        photo_sched = safe(photo_res)
        quote       = _get_quote(now_msk)

        # Сборка дайджеста
        parts: list[str] = []

        # Заголовок
        day_name = _DAY_RU[now_msk.weekday()]
        parts.append(
            f"☀️ *Доброе утро!* {day_name}, {now_msk.strftime('%d.%m.%Y')}\n"
        )

        # Погода
        if weather:
            parts.append(f"🌤 *Погода*\n{weather}\n")

        # Расписание — приоритет: фото-расписание → calendar events
        schedule = photo_sched or events
        if schedule:
            parts.append(f"📅 *Расписание*\n{schedule}\n")

        # Задачи
        if tasks:
            parts.append(f"✅ *Задачи*\n{tasks}\n")

        # Новости
        if news:
            parts.append(news)

        # Философская цитата
        if quote:
            parts.append(f"\n💬 _{quote}_\n")

        if len(parts) <= 2:
            text = "🌅 Доброе утро! Не удалось загрузить дайджест — проверь подключение."
        else:
            text = "\n".join(parts)

        return AgentResult(
            success=True,
            content=text,
            agent_name=self.agent_name,
            needs_critic=False,
        )


# ── Погода ────────────────────────────────────────────────────────────────────

async def _get_weather(user_id: int) -> str:
    """
    Погода через wttr.in для города из Core Memory пользователя.
    Если города нет — запрашивает без указания города (IP-геолокация wttr.in).
    """
    try:
        import httpx
        from app.llm_service import _get_user_city

        city = _get_user_city(user_id)
        url  = _WEATHER_API.format(city=city) if city else _WEATHER_API_ANON

        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.get(url)
            r.raise_for_status()
            data = r.json()

        cur  = data["current_condition"][0]
        fc   = data["weather"][0]

        temp   = int(cur["temp_C"])
        feels  = int(cur["FeelsLikeC"])
        wind   = int(cur["windspeedKmph"])
        humid  = int(cur["humidity"])
        desc   = (
            cur.get("lang_ru", [{}])[0].get("value")
            or cur["weatherDesc"][0]["value"]
        )
        max_t  = int(fc["maxtempC"])
        min_t  = int(fc["mintempC"])
        precip = float(
            next((h.get("precipMM", 0) for h in fc.get("hourly", []) if int(h.get("time", 0)) >= 600), 0)
        )

        # Совет
        tip = (
            "одевайся тепло" if temp < -10 else
            "мороз, тепло"   if temp < 0   else
            "куртка"          if temp < 8   else
            "лёгкая куртка"   if temp < 16  else
            "кофта"           if temp < 22  else
            "налегке"
        )
        if precip > 0.5:
            tip += ", зонт"
        if wind > 30:
            tip += ", ветрено"

        city_label = f" ({city})" if city else ""
        rain_str   = f", осадки {precip:.1f}мм" if precip > 0.5 else ""

        return (
            f"{desc}{city_label}. {temp}°C, ощущается {feels}°C.\n"
            f"День: {min_t}…{max_t}°C. Влажность {humid}%{rain_str}.\n"
            f"→ {tip}."
        )
    except Exception as e:
        logger.warning("Погода: %s", e)
        return ""


# ── Расписание ────────────────────────────────────────────────────────────────

async def _get_events(user_id: int, now_msk: datetime) -> str:
    """События сегодня + завтра из calendar_service."""
    try:
        from app.database import _conn

        today    = now_msk.strftime("%Y-%m-%d")
        tomorrow = (now_msk + timedelta(days=1)).strftime("%Y-%m-%d")

        _EMOJI = {"red": "🔴", "green": "🟢", "yellow": "🟡",
                  "purple": "🟣", "blue": "🔵"}

        with _conn() as con:
            rows_today = con.execute(
                """SELECT time_start, time_end, title, color, description
                   FROM events WHERE user_id=? AND date=?
                   ORDER BY CASE WHEN time_start IS NULL THEN '99:99' ELSE time_start END""",
                (user_id, today),
            ).fetchall()
            rows_tomorrow = con.execute(
                """SELECT time_start, title, color
                   FROM events WHERE user_id=? AND date=?
                   ORDER BY CASE WHEN time_start IS NULL THEN '99:99' ELSE time_start END""",
                (user_id, tomorrow),
            ).fetchall()

        if not rows_today and not rows_tomorrow:
            return "Событий нет."

        lines: list[str] = []

        if rows_today:
            lines.append("_Сегодня:_")
            for r in rows_today:
                t     = f" {r[0]}" if r[0] else ""
                t_end = f"–{r[1]}" if r[1] else ""
                emoji = _EMOJI.get(r[3] or "", "🔵")
                desc  = f"\n    ↳ {r[4]}" if r[4] else ""
                lines.append(f"  {emoji}{t}{t_end} {r[2]}{desc}")

        if rows_tomorrow:
            lines.append("_Завтра:_")
            for r in rows_tomorrow:
                t     = f" {r[0]}" if r[0] else ""
                emoji = _EMOJI.get(r[2] or "", "🔵")
                lines.append(f"  {emoji}{t} {r[1]}")

        return "\n".join(lines)
    except Exception as e:
        logger.warning("События: %s", e)
        return ""


# ── Задачи ────────────────────────────────────────────────────────────────────

async def _get_tasks(user_id: int) -> str:
    """Активные задачи с приоритизацией: просроченные → сегодня → ближайшие."""
    try:
        from app.database import _conn

        today = utcnow().strftime("%Y-%m-%d")

        with _conn() as con:
            rows = con.execute(
                """SELECT text, due_date, priority
                   FROM tasks
                   WHERE user_id=? AND done=0
                   ORDER BY
                     CASE WHEN due_date != '' AND due_date < ? THEN 0
                          WHEN due_date = ? THEN 1
                          WHEN due_date IS NULL OR due_date = '' THEN 3
                          ELSE 2 END,
                     priority ASC,
                     due_date ASC
                   LIMIT 8""",
                (user_id, today, today),
            ).fetchall()

        if not rows:
            return "Активных задач нет. 🎉"

        lines: list[str] = []
        for text, due_date, priority in rows:
            if due_date and due_date < today:
                prefix = "🔴 просрочено"
            elif due_date == today:
                prefix = "🟡 сегодня"
            elif due_date:
                prefix = f"📌 {due_date}"
            else:
                prefix = "⬜"
            lines.append(f"  {prefix} {text}")

        return "\n".join(lines)
    except Exception as e:
        logger.warning("Задачи: %s", e)
        return ""


# ── Новости ───────────────────────────────────────────────────────────────────

async def _get_news_digest() -> str:
    """
    1. Параллельный поиск по трём блокам (AI, IT, Мир) через Tavily.
    2. Один LLM-вызов (router_model) для пересказа — экономим токены.
    3. Акцент: последние 24 часа, конкретика (компании, имена, числа).
    """
    try:
        from deeper.services.web_search import WebSearch
        from deeper.config import deeper_config

        if not deeper_config.tavily_api_key:
            return ""

        svc  = WebSearch(api_key=deeper_config.tavily_api_key, pages_per_query=3)
        date = utcnow().strftime("%d %B %Y")

        # Собираем задачи: по два лучших запроса на блок
        tasks_map: dict[str, list] = {}
        for block, queries in _NEWS_QUERIES.items():
            tasks_map[block] = [
                svc.search_query(f"{q} {date}") for q in queries[:2]
            ]

        # Параллельный поиск всех запросов
        all_tasks = [(block, coro)
                     for block, coros in tasks_map.items()
                     for coro in coros]
        results = await asyncio.gather(
            *[coro for _, coro in all_tasks],
            return_exceptions=True,
        )

        # Группируем сниппеты по блоку
        block_texts: dict[str, list[str]] = {}
        for (block, _), result in zip(all_tasks, results):
            if isinstance(result, Exception) or not result:
                continue
            bucket = block_texts.setdefault(block, [])
            # answers — Tavily AI-синтез, самое ценное
            for ans in (result.get("answers") or [])[:2]:
                if ans and len(ans) > 30:
                    bucket.append(ans[:400])
            # snippets — сырые фрагменты
            for snip in (result.get("snippets") or [])[:3]:
                if snip and len(snip) > 40:
                    bucket.append(snip[:300])

        if not any(block_texts.values()):
            return ""

        # Формируем контекст для LLM
        raw_parts: list[str] = []
        for block, texts in block_texts.items():
            if texts:
                combined = "\n".join(f"- {t}" for t in texts[:6])
                raw_parts.append(f"{block}\n{combined}")

        raw_combined = "\n\n".join(raw_parts)

        # Один LLM-вызов — router_model достаточно для пересказа
        llm = ChatGroq(
            api_key=settings.groq_api_key,
            model=settings.router_model,
            temperature=0.15,
        )

        response = await llm.ainvoke([
            SystemMessage(content=(
                "Ты редактор новостного дайджеста. Пишешь кратко, конкретно, без воды. "
                "Обязательно: компании, имена людей, конкретные числа/версии/суммы. "
                "Никаких URL. Никаких вводных фраз типа 'В мире технологий...'. "
                "Максимум 3 предложения на блок."
            )),
            HumanMessage(content=(
                f"Сегодня {date}. Вот сырые данные из поиска.\n"
                "Напиши дайджест. Сохрани эмодзи-заголовки блоков.\n"
                "Если данных по блоку нет или они несвежие — пропусти блок.\n\n"
                f"{raw_combined}"
            )),
        ])

        summary = str(response.content).strip()
        if not summary or len(summary) < 50:
            return ""

        return f"📰 *Новости за последние 24 часа*\n\n{summary}"

    except Exception as e:
        logger.warning("Новости: %s", e)
        return ""


# ── Расписание из фото ────────────────────────────────────────────────────────

async def _get_photo_schedule(user_id: int) -> str:
    """Загружает расписание сохранённое из фото."""
    try:
        from app.database import get_schedule_photo
        sched = get_schedule_photo(user_id)
        if not sched or not sched.get("raw_text"):
            return ""
        updated = sched.get("updated_at", "")[:10]
        return f"**📅 Твоё расписание** (фото от {updated}):\n{sched['raw_text'][:600]}"
    except Exception as e:
        logger.debug("photo schedule: %s", e)
        return ""


# ── Цитата ────────────────────────────────────────────────────────────────────

def _get_quote(now: datetime) -> str:
    """Детерминированная цитата по дню года — каждый день новая."""
    author, text = _QUOTES[now.timetuple().tm_yday % len(_QUOTES)]
    return f"«{text}» — {author}"


# ── Вспомогательное ───────────────────────────────────────────────────────────

_DAY_RU = [
    "Понедельник", "Вторник", "Среда", "Четверг",
    "Пятница", "Суббота", "Воскресенье",
]
