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
_NEWS_QUERIES = [
    # Технологии и AI
    ("AI technology breakthroughs today", "🤖 AI и технологии"),
    ("artificial intelligence news today", "🤖 AI и технологии"),

    # Бизнес и стартапы
    ("startup funding tech business news today", "💼 Бизнес"),
    ("entrepreneurship business world news today", "💼 Бизнес"),

    # Наука
    ("science discovery research news today", "🔬 Наука"),
    ("space exploration science breakthrough today", "🔬 Наука"),

    # Мировые события
    ("world news important events today", "🌍 Мир"),
    ("global economy geopolitics today", "🌍 Мир"),

    # Продуктивность и психология
    ("productivity psychology habits research today", "🧠 Психология"),
    ("mental performance focus research today", "🧠 Психология"),
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

        weather, tasks_text, news_text = await asyncio.gather(
            weather_task, tasks_task, news_task, return_exceptions=True
        )

        if isinstance(weather, Exception):    weather    = ""
        if isinstance(tasks_text, Exception): tasks_text = ""
        if isinstance(news_text, Exception):  news_text  = ""

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

        lines.append(f"_{quote}_\n— {author}\n")

        lines.append("**На подумать:**")
        for idea in philosophy_choices:
            lines.append(f"• {idea}")

        if news_text:
            lines.append(f"\n{news_text}")

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
        from app.integrations.obsidian import get_all_tasks, vault_available
        from app.database import get_active_tasks
        lines = []
        if vault_available():
            all_tasks = get_all_tasks()
            q1 = [t["text"] for t in all_tasks["q1"]["tasks"] if not t["done"]]
            q2 = [t["text"] for t in all_tasks["q2"]["tasks"] if not t["done"]]
            if q1:
                lines.append("**Срочно:**")
                for t in q1[:3]:
                    lines.append(f"• {t}")
            if q2:
                lines.append("**Важно:**")
                for t in q2[:3]:
                    lines.append(f"• {t}")
            total = sum(len([t for t in d["tasks"] if not t["done"]]) for d in all_tasks.values())
            if total > 6:
                lines.append(f"_...и ещё {total - 6} задач_")
        else:
            db_tasks = get_active_tasks(user_id)
            if db_tasks:
                lines.append("**Задачи:**")
                for t in db_tasks[:4]:
                    lines.append(f"• {t[1]}")
        return "\n".join(lines) + "\n" if lines else ""
    except Exception as e:
        logger.warning("Задачи: %s", e)
        return ""


# ── Новости ───────────────────────────────────────────────────────────────────

async def _get_news() -> str:
    """
    Параллельный поиск по 4 темам через Tavily.
    Берёт 2 рандомных темы чтобы каждый день было разное.
    """
    try:
        from app.search_service import SearchService

        svc = SearchService()

        # Выбираем 4 рандомных темы из пула
        selected = random.sample(_NEWS_QUERIES, min(4, len(_NEWS_QUERIES)))

        # Параллельный поиск
        results = await asyncio.gather(
            *[svc.search(query, max_results=2) for query, _ in selected],
            return_exceptions=True
        )

        sections = []
        for (query, label), result in zip(selected, results):
            if isinstance(result, Exception) or not result:
                continue
            # result — отформатированная строка от search_service
            text = str(result).strip()
            if len(text) < 30:
                continue
            # Берём первые 400 символов
            preview = text[:400].rstrip()
            if len(text) > 400:
                preview += "..."
            sections.append(f"**{label}**\n{preview}")

        if not sections:
            return ""

        header = "─" * 20
        return f"\n{header}\n**📰 Дайджест дня**\n\n" + "\n\n".join(sections)

    except Exception as e:
        logger.warning("Новости: %s", e)
        return ""


def _day_ru(dt: datetime) -> str:
    days = ["Понедельник", "Вторник", "Среда", "Четверг",
            "Пятница", "Суббота", "Воскресенье"]
    return days[dt.weekday()]
