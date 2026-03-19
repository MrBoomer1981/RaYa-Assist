"""
morning_agent.py — утренний дайджест.

Срабатывает в 6:45 МСК. Только из ProactiveService.

Формат (сухой, без воды):
  День + дата
  Погода — точные цифры
  Задачи — Q1 первые, потом Q2
  Цитата — одна, без автора в скобках
  Философия — 2-3 идеи на поразмышлять
"""
import asyncio
import logging
import random
from datetime import datetime

from langchain_core.messages import HumanMessage, SystemMessage

from app.agents.base_agent import AgentContext, AgentResult, BaseAgent

logger = logging.getLogger(__name__)

_WEATHER_API = "https://wttr.in/Samara?format=j1"

# ── Цитаты ────────────────────────────────────────────────────────────────────
_QUOTES = [
    ("Делай что должен, и будь что будет.", "Марк Аврелий"),
    ("Человек — это то, что он делает повторно.", "Аристотель"),
    ("Препятствие на пути становится путём.", "Марк Аврелий"),
    ("Кто знает других — мудр. Кто знает себя — просветлён.", "Лао-цзы"),
    ("Не жди. Никогда не будет подходящего времени.", "Наполеон Хилл"),
    ("Сложность — враг исполнения.", "Тони Роббинс"),
    ("Большинство людей переоценивают год и недооценивают десять лет.", "Билл Гейтс"),
    ("Амбиции без знаний — как лодка без руля.", "Харви Маккей"),
    ("Единственный способ делать великую работу — любить то, что делаешь.", "Стив Джобс"),
    ("Боль временна. Сдаться — навсегда.", "Лэнс Армстронг"),
    ("Не объясняй. Твои друзья не нуждаются, а враги не поверят.", "Эльберт Хаббард"),
    ("Дисциплина — это выбор между тем, чего ты хочешь сейчас, и тем, чего хочешь больше всего.", "Abraham Lincoln"),
    ("Всё достигнутое мной — результат того, что я делал то, чего не хотел делать.", "Томас Эдисон"),
    ("Неважно как медленно ты идёшь — главное не останавливаться.", "Конфуций"),
    ("Мудрость начинается с удивления.", "Сократ"),
    ("Качество — это не случайность. Это всегда результат разумных усилий.", "Джон Раскин"),
    ("Человек, который никогда не ошибался, никогда не пробовал ничего нового.", "Эйнштейн"),
    ("Простота — это высшая изощрённость.", "Леонардо да Винчи"),
    ("Лучшее время посадить дерево — 20 лет назад. Второе лучшее — сейчас.", "Китайская пословица"),
    ("Ты становишься тем, о чём думаешь большую часть времени.", "Эрл Найтингейл"),
]

# ── Философские идеи ──────────────────────────────────────────────────────────
_PHILOSOPHY = [
    "Ты принимаешь десятки решений автоматически. Какое из них стоит пересмотреть?",
    "Дискомфорт сейчас vs сожаление потом. Что ты выбираешь сегодня?",
    "Если бы у тебя было вдвое меньше времени — что бы ты убрал первым?",
    "Страх ошибки часто дороже самой ошибки.",
    "Чем занимается твоё внимание, когда ты не контролируешь его?",
    "Есть ли у тебя задача, которую ты всё время откладываешь — не потому что сложно, а потому что важно?",
    "Ты работаешь над проблемой или притворяешься что работаешь?",
    "Кто бы ты был через 5 лет если бы делал противоположное тому что делаешь сейчас?",
    "Сложность — это часто признак что мы не до конца понимаем задачу.",
    "Привычка сильнее мотивации. Что ты строишь прямо сейчас — привычку или порыв?",
    "Разница между занятостью и продуктивностью — в результатах.",
    "Если задача кажется огромной — возможно ты смотришь на неё целиком вместо первого шага.",
    "Что бы ты посоветовал другу с твоими же проблемами?",
    "Ты реагируешь на мир или создаёшь его?",
    "Энергия, которую ты тратишь на объяснение почему не можешь — можно потратить на то чтобы начать.",
    "Какое решение ты уже знаешь но избегаешь принять?",
    "Оптимизм без плана — это мечта. Пессимизм с планом — это стратегия.",
    "Ты можешь контролировать усилие, но не результат. Фокусируйся на правильном.",
    "Большинство проблем исчезают если просто начать.",
    "Стоимость ничегонеделания всегда выше стоимости ошибки.",
]


class MorningAgent(BaseAgent):
    agent_name = "morning"
    timeout    = 40

    def _system_prompt(self) -> str:
        return ""

    async def _execute(self, ctx: AgentContext) -> AgentResult:
        now = datetime.utcnow()

        # Параллельно: погода + задачи + поиск философии дня
        weather_task = asyncio.create_task(_get_weather())
        tasks_task   = asyncio.create_task(_get_tasks(ctx.user_id))

        weather, tasks_text = await asyncio.gather(
            weather_task, tasks_task, return_exceptions=True
        )

        if isinstance(weather, Exception):
            weather = ""
        if isinstance(tasks_text, Exception):
            tasks_text = ""

        # Цитата и философия
        quote, author   = random.choice(_QUOTES)
        philosophy_pool = random.sample(_PHILOSOPHY, 3)

        # Формируем дайджест
        day    = _day_ru(now)
        date   = now.strftime("%d.%m")
        lines  = []

        # Заголовок
        lines.append(f"**{day}, {date}**\n")

        # Погода
        if weather:
            lines.append(f"☁ {weather}\n")

        # Задачи
        if tasks_text:
            lines.append(tasks_text)

        # Цитата
        lines.append(f'_{quote}_\n— {author}\n')

        # Философия
        lines.append("**На подумать:**")
        for idea in philosophy_pool:
            lines.append(f"• {idea}")

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

        cur     = data["current_condition"][0]
        forecast_today = data["weather"][0]  # сегодняшний прогноз

        temp    = int(cur["temp_C"])
        feels   = int(cur["FeelsLikeC"])
        desc    = (cur.get("lang_ru", [{}])[0].get("value")
                   or cur["weatherDesc"][0]["value"])
        wind    = int(cur["windspeedKmph"])
        humid   = int(cur["humidity"])

        max_t   = int(forecast_today["maxtempC"])
        min_t   = int(forecast_today["mintempC"])

        # Осадки
        precip  = float(forecast_today.get("hourly", [{}])[6].get("precipMM", 0))
        rain_str = f", дождь {precip:.1f}мм" if precip > 0.5 else ""

        # Совет
        if temp < -10:       tip = "одевайся тепло"
        elif temp < 0:       tip = "мороз, тепло"
        elif temp < 8:       tip = "куртка"
        elif temp < 16:      tip = "лёгкая куртка"
        elif temp < 22:      tip = "кофта"
        else:                tip = "налегке"

        if precip > 0.5:     tip += ", зонт"
        if wind > 30:        tip += ", ветрено"

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

            total = sum(
                len([t for t in d["tasks"] if not t["done"]])
                for d in all_tasks.values()
            )
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


def _day_ru(dt: datetime) -> str:
    days = ["Понедельник", "Вторник", "Среда", "Четверг",
            "Пятница", "Суббота", "Воскресенье"]
    return days[dt.weekday()]
