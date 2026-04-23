"""
morning_agent.py — утренний дайджест.

Срабатывает в 6:45 МСК. Только из ProactiveService.

Формат:
  День + дата
  Погода — точные цифры
  Задачи — Q1 первые, потом Q2
  Цитата — одна, живая
  Философия — 2-3 идеи (глубже, не банальности)
  Новости — параллельный поиск по 4 темам через Tavily
"""
import asyncio
import logging
import random
from datetime import datetime

from app.agents.base_agent import AgentContext, AgentResult, BaseAgent

logger = logging.getLogger(__name__)

_WEATHER_API = "https://wttr.in/Samara?format=j1"

# ── Цитаты — живые, не корпоративные ─────────────────────────────────────────
_QUOTES = [
    ("Тот, кто знает — не говорит. Тот, кто говорит — не знает.", "Лао-цзы"),
    ("Мы страдаем больше в воображении, чем в реальности.", "Сенека"),
    ("Memento mori. Помни о смерти — и живи полнее.", "Stoics"),
    ("Единственный выход — насквозь.", "Роберт Фрост"),
    ("Не жалуйся на тьму — зажги свечу.", "Конфуций"),
    ("Человек не вещь, а процесс.", "Эрих Фромм"),
    ("Мы то, что мы делаем снова и снова.", "Аристотель"),
    ("Настоящее — это всё что у тебя есть. И этого достаточно.", "Марк Аврелий"),
    ("Препятствие — это путь.", "Дзен"),
    ("Счастье — это не то что ты получаешь, а то каким ты становишься.", "Джим Рон"),
    ("Боишься — сделай это боясь.", "Сьюзен Джефферс"),
    ("Не имей сто рублей, а имей сто мыслей.", "Народная мудрость"),
    ("Великие умы обсуждают идеи. Средние — события. Мелкие — людей.", "Элеонор Рузвельт"),
    ("Слабый человек говорит что-то случилось. Сильный — что он сделал что-то.", "Эпиктет"),
    ("Твоя жизнь — это то на что ты обращаешь внимание.", "Уильям Джеймс"),
    ("Ничто не имеет смысла само по себе. Смысл даёшь ты.", "Виктор Франкл"),
    ("Амбиция без направления — это хаос.", "Питер Друкер"),
    ("Если ты не можешь объяснить просто — ты не понимаешь достаточно хорошо.", "Фейнман"),
    ("Ты не видишь мир таким, каков он есть. Ты видишь его таким, каков ты.", "Талмуд"),
    ("Скука — это тревога без содержания.", "Пол Тиллих"),
    ("Одиночество необходимо для того, кто хочет думать.", "Паскаль"),
    ("Вся беда человека не в том, что он не знает — а в том что знает наверняка.", "Марк Твен"),
    ("Лучше сделать и пожалеть, чем не сделать и пожалеть.", "Джованни Боккаччо"),
    ("Первый признак невежды — уверенность.", "Бертран Рассел"),
    ("Люди не идеи. Но идеи меняют людей.", "Достоевский"),
    ("Настоящая щедрость по отношению к будущему — отдать всё настоящему.", "Камю"),
    ("Читай чтобы жить.", "Флобер"),
    ("Кто смотрит наружу — спит. Кто смотрит внутрь — просыпается.", "Юнг"),
    ("Опыт — это название которое мы даём своим ошибкам.", "Оскар Уайльд"),
    ("Деньги не сделают тебя счастливым. Но дадут тебе лучший вид на твои несчастья.", "Довлатов"),
]

# ── Философия — глубже, острее, не банальности ───────────────────────────────
_PHILOSOPHY = [
    # Экзистенциальное
    "Если бы ты умер завтра — что из незаконченного тебя бы беспокоило больше всего?",
    "Большинство людей живут чужой жизнью. Насколько твоя жизнь — твоя?",
    "Ты избегаешь думать о смерти. А ведь именно она придаёт смысл каждому дню.",
    "Страдание неизбежно. Но большую часть своих страданий мы создаём сами.",
    "Свобода пугает больше чем несвобода. Ты это замечаешь?",

    # Мышление и восприятие
    "Твоя карта — это не территория. Насколько точна твоя карта реальности?",
    "Ты думаешь своими мыслями или мыслями тех, кого читал последние 30 дней?",
    "Последний раз когда ты изменил своё мнение под влиянием аргументов — когда это было?",
    "Самая опасная фраза: 'мы всегда так делали'. Где ты её используешь?",
    "Что ты считаешь правдой только потому что все вокруг так считают?",

    # Действие и прокрастинация
    "Что ты откладываешь не потому что сложно, а потому что страшно?",
    "Твоя прокрастинация — это не лень. Это сопротивление. Чему именно?",
    "Идеальный момент никогда не наступит. Какой худший момент лучше чем этот?",
    "Ты готовишься к жизни или живёшь? Разница принципиальная.",
    "Разрыв между знанием и действием — где он у тебя самый большой?",

    # Отношения и общество
    "Кто из твоего окружения тянет тебя вниз? Ты это признаёшь себе?",
    "Ты слушаешь чтобы понять или чтобы ответить?",
    "Какую роль ты играешь которая давно перестала быть тобой?",
    "Твои ценности и твои действия — насколько они совпадают?",

    # Парадоксы
    "Чем больше ты контролируешь — тем меньше живёшь.",
    "Люди тратят здоровье чтобы заработать деньги. Потом деньги чтобы вернуть здоровье.",
    "Ты хочешь быть правым или хочешь быть счастливым? Иногда надо выбирать.",
    "Чего бы ты не делал даже за очень большие деньги?",

    # Системное мышление
    "Система всегда побеждает намерение. Какие системы ты строишь вокруг себя?",
    "Твои привычки — это автопилот. Куда он тебя летит?",
    "Проблема редко там где кажется. Где настоящая причина того что тебя беспокоит?",
    "Что изменится в твоей жизни через год если ты ничего не изменишь сейчас?",
]

# ── Темы для поиска новостей ──────────────────────────────────────────────────
# Фиксированный набор — все 5 тем каждый день, никакого рандома.
# Запросы конструируются с датой в _get_news() чтобы гарантировать свежесть.
_NEWS_TOPICS = [
    ("AI искусственный интеллект новости",            "🤖 AI и технологии"),
    ("главные мировые события",                        "🌍 Мир"),
    ("бизнес технологии стартапы",                     "💼 Бизнес"),
    ("наука исследования открытия",                    "🔬 Наука"),
    ("психология продуктивность мышление исследования","🧠 Психология"),
]


class MorningAgent(BaseAgent):
    agent_name = "morning"
    timeout    = 60

    def _system_prompt(self) -> str:
        return ""

    async def _execute(self, ctx: AgentContext) -> AgentResult:
        now = datetime.utcnow()

        # Параллельный сбор всего
        weather_task = asyncio.create_task(_get_weather())
        tasks_task   = asyncio.create_task(_get_tasks(ctx.user_id))
        news_task    = asyncio.create_task(_get_news())

        events_task  = asyncio.create_task(_get_events(ctx.user_id))
        social_task  = asyncio.create_task(_get_social_trends())

        weather, tasks_text, news_text, events_text, social_text = await asyncio.gather(
            weather_task, tasks_task, news_task, events_task, social_task,
            return_exceptions=True,
        )

        if isinstance(weather, Exception):      weather      = ""
        if isinstance(tasks_text, Exception):   tasks_text   = ""
        if isinstance(news_text, Exception):    news_text    = ""
        if isinstance(events_text, Exception):  events_text  = ""
        if isinstance(social_text, Exception):  social_text  = ""

        # Цитата и философия
        quote, author      = random.choice(_QUOTES)
        philosophy_choices = random.sample(_PHILOSOPHY, 3)

        # Формируем дайджест
        day    = _day_ru(now)
        date   = now.strftime("%d.%m")
        lines  = [f"**{day}, {date}**\n"]

        if weather:
            lines.append(f"☁ {weather}\n")

        if tasks_text:
            lines.append(tasks_text)

        if events_text:
            lines.append(events_text)

        lines.append(f"_{quote}_\n— {author}\n")

        lines.append("**На подумать:**")
        for idea in philosophy_choices:
            lines.append(f"• {idea}")

        if news_text:
            lines.append(f"\n{news_text}")

        if social_text:
            lines.append(f"\n{social_text}")

        return AgentResult(
            success=True,
            content="\n".join(lines),
            agent_name=self.agent_name,
            needs_critic=False,
        )


# ── Погода ────────────────────────────────────────────────────────────────────

async def _get_weather() -> str:
    try:
        import httpx
        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.get(_WEATHER_API)
            r.raise_for_status()
            data = r.json()

        cur            = data["current_condition"][0]
        forecast_today = data["weather"][0]
        temp           = int(cur["temp_C"])
        feels          = int(cur["FeelsLikeC"])
        desc           = (cur.get("lang_ru", [{}])[0].get("value")
                          or cur["weatherDesc"][0]["value"])
        wind           = int(cur["windspeedKmph"])
        humid          = int(cur["humidity"])
        max_t          = int(forecast_today["maxtempC"])
        min_t          = int(forecast_today["mintempC"])
        precip         = float(forecast_today.get("hourly", [{}])[6].get("precipMM", 0))
        rain_str       = f", дождь {precip:.1f}мм" if precip > 0.5 else ""

        if temp < -10:    tip = "одевайся тепло"
        elif temp < 0:    tip = "мороз, тепло"
        elif temp < 8:    tip = "куртка"
        elif temp < 16:   tip = "лёгкая куртка"
        elif temp < 22:   tip = "кофта"
        else:             tip = "налегке"
        if precip > 0.5:  tip += ", зонт"
        if wind > 30:     tip += ", ветрено"

        return (f"{desc}. {temp}°C, ощущается {feels}°C. "
                f"День: {min_t}…{max_t}°C. "
                f"Влажность {humid}%{rain_str}. {tip}.")
    except Exception as e:
        logger.warning("Погода: %s", e)
        return ""


# ── Задачи ────────────────────────────────────────────────────────────────────

async def _get_tasks(user_id: int) -> str:
    try:
        from app.database import get_active_tasks
        db_tasks = get_active_tasks(user_id)
        if not db_tasks:
            return ""
        emoji = {1: "🔴", 2: "🟡", 3: "🟠"}
        lines = ["**Задачи на сегодня:**"]
        for t in db_tasks[:5]:
            e = emoji.get(t[2], "🟡")
            lines.append(f"{e} {t[1]}")
        if len(db_tasks) > 5:
            lines.append(f"_...и ещё {len(db_tasks) - 5} задач_")
        return "\n".join(lines) + "\n"
    except Exception as e:
        logger.warning("Задачи: %s", e)
        return ""


# ── События календаря ─────────────────────────────────────────────────────────

async def _get_events(user_id: int) -> str:
    """Ближайшие события на сегодня и завтра для дайджеста."""
    try:
        from app.database import get_events_for_date
        from datetime import datetime, timedelta
        today    = datetime.utcnow().date()
        tomorrow = today + timedelta(days=1)

        ev_today    = get_events_for_date(user_id, today.isoformat())
        ev_tomorrow = get_events_for_date(user_id, tomorrow.isoformat())

        if not ev_today and not ev_tomorrow:
            return ""

        _EMOJI = {"blue": "🔵", "green": "🟢", "red": "🔴", "orange": "🟠", "purple": "🟣"}
        lines  = ["**📅 Календарь:**"]

        if ev_today:
            lines.append("_Сегодня:_")
            for ev in ev_today:
                t = f" {ev['time_start']}" if ev["time_start"] else ""
                lines.append(f"  {_EMOJI.get(ev['color'], '🔵')}{t} {ev['title']}")

        if ev_tomorrow:
            lines.append("_Завтра:_")
            for ev in ev_tomorrow:
                t = f" {ev['time_start']}" if ev["time_start"] else ""
                lines.append(f"  {_EMOJI.get(ev['color'], '🔵')}{t} {ev['title']}")

        return "\n".join(lines) + "\n"
    except Exception as e:
        logger.warning("События: %s", e)
        return ""


# ── Новости ───────────────────────────────────────────────────────────────────

async def _get_news() -> str:
    """
    Параллельный поиск по 5 фиксированным темам через news_search (Tavily topic=news).
    Запросы содержат дату → только свежие результаты за последние сутки.
    Пересказывается через быстрый LLM с деталями: кто, что, когда.
    """
    try:
        from app.search_service import SearchService
        from langchain_groq import ChatGroq
        from langchain_core.messages import SystemMessage, HumanMessage
        from app.config import settings

        svc  = SearchService()
        now  = datetime.utcnow()
        date = now.strftime("%d %B %Y")  # напр. "22 April 2026"

        # Обогащаем запрос датой — поисковик ранжирует свежее выше архивного
        queries_with_date = [
            (f"{q} {date}", label)
            for q, label in _NEWS_TOPICS
        ]

        # Параллельный поиск — news_search использует Tavily topic=news + DDG fallback
        results = await asyncio.gather(
            *[svc.news_search(query, max_results=4) for query, _ in queries_with_date],
            return_exceptions=True,
        )

        raw_sections = []
        for (_, label), result in zip(queries_with_date, results):
            if isinstance(result, Exception) or not result:
                continue
            text = str(result).strip()
            if len(text) < 40:
                continue
            raw_sections.append((label, text[:1200]))  # больше текста → лучше пересказ

        if not raw_sections:
            return ""

        raw_combined = "\n\n".join(
            f"[{label}]\n{text}" for label, text in raw_sections
        )

        llm = ChatGroq(
            api_key=settings.groq_api_key,
            model=settings.router_model,  # быстрая модель, не тратим основную
            temperature=0.2,
        )

        prompt = (
            f"Сегодня {date}. Ниже — результаты поиска новостей за последние сутки по 5 темам.\n"
            "Для каждой темы напиши ИНФОРМАТИВНЫЙ пересказ: 2-3 предложения с конкретикой — "
            "названия компаний, имена, цифры, события. Не пиши общими словами.\n"
            "Если новостей по теме нет или они неактуальны — пропусти тему.\n"
            "Сохрани эмодзи-метку. Никаких URL. Только суть.\n\n"
            "Формат:\n"
            "🤖 AI и технологии\n<2-3 предложения>\n\n"
            "🌍 Мир\n<2-3 предложения>\n\nи т.д.\n\n"
            f"Исходные данные:\n{raw_combined}"
        )

        response = await llm.ainvoke([
            SystemMessage(content=(
                "Ты редактор новостного дайджеста. Пишешь кратко, конкретно, без воды. "
                "Факты: имена, цифры, компании, события. Никаких URL."
            )),
            HumanMessage(content=prompt),
        ])
        summary = str(response.content).strip()

        if not summary:
            return ""

        header = "─" * 20
        return f"\n{header}\n**📰 Новости за последние сутки**\n\n{summary}"

    except Exception as e:
        logger.warning("Новости: %s", e)
        return ""


async def _get_social_trends() -> str:
    """Горячее с Reddit и X для утреннего дайджеста."""
    try:
        from app.search_service import SocialSearchService
        svc = SocialSearchService()
        result = await svc.trending_digest(topics=["tech", "news"], reddit_count=3, x_count=2)
        return result
    except Exception as e:
        logger.warning("Social trends: %s", e)
        return ""


def _day_ru(dt: datetime) -> str:
    days = ["Понедельник", "Вторник", "Среда", "Четверг",
            "Пятница", "Суббота", "Воскресенье"]
    return days[dt.weekday()]
