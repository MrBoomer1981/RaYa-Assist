"""
morning_agent.py — утренний дайджест RaYa.

Срабатывает ОДИН РАЗ в день в 6:45 МСК (03:45 UTC).
Вызывается только из ProactiveService — не роутером.

Акцент: живой, разговорный дайджест от RaYa лично для Сократа.
Никаких списков, никаких URL. Как будто говорит подруга за кофе.
"""
import asyncio
import logging
from datetime import datetime

from langchain_core.messages import HumanMessage, SystemMessage

from app.agents.base_agent import AgentContext, AgentResult, BaseAgent
from app.config import settings
from app.database import (
    get_active_reminders,
    get_active_tasks,
    get_recent_moods,
    load_diary_entries,
)

logger = logging.getLogger(__name__)

_WEATHER_API = "https://wttr.in/Samara?format=j1&lang=ru"

_SYSTEM = """\
Ты RaYa. Сейчас 6:45 утра в Самаре. Сократ только проснулся.

Напиши ему утреннее сообщение — живое, тёплое, личное.
Говори как близкий друг, а не как новостной агрегатор.

Структура (не нумеруй, не делай заголовки):
1. Короткое приветствие — с учётом дня недели, настроением (если знаешь)
2. Погода — одной фразой, что надеть. Никаких цифр без контекста
3. Самое важное из задач — только одна, самая важная. Не список
4. Если есть незакрытые темы из вчерашнего разговора — напомни одним предложением
5. Одна мысль, вопрос или наблюдение от себя — что-то личное, не шаблонное

Правила которые нельзя нарушать:
- Никаких URL и ссылок
- Никаких заголовков и нумерации
- Не более 150 слов — Сократ только проснулся
- Тон живой, не деловой
- Обращаться только "Сократ"
- Никаких "Доброе утро!" в начале — скучно и шаблонно
- Заканчивай вопросом или короткой фразой которая даёт энергию"""


class MorningAgent(BaseAgent):
    agent_name = "morning"
    timeout    = 40

    def _system_prompt(self) -> str:
        return _SYSTEM

    async def _execute(self, ctx: AgentContext) -> AgentResult:
        now = datetime.utcnow()

        # Собираем всё параллельно
        weather_task = asyncio.create_task(_get_weather())

        news_task = None
        if settings.search_enabled:
            from app.search_service import SearchService
            news_task = asyncio.create_task(
                SearchService().search("технологии AI новости сегодня")
            )

        tasks     = get_active_tasks(ctx.user_id)
        reminders = get_active_reminders(ctx.user_id)
        moods     = get_recent_moods(ctx.user_id, limit=3)
        diary     = load_diary_entries(ctx.user_id, limit=1)

        weather = await weather_task
        news    = ""
        if news_task:
            try:
                news = await news_task
            except Exception:
                pass

        today             = now.strftime("%Y-%m-%d")
        todays_reminders  = [r for r in reminders if r[2].startswith(today)]
        day_of_week       = _day_ru(now)
        priority_map      = {1: "срочная", 2: "обычная", 3: "низкий приоритет"}

        # Формируем контекст — только факты, без форматирования
        parts = [f"День: {day_of_week}"]

        if weather:
            parts.append(f"Погода в Самаре: {weather}")

        if tasks:
            top = tasks[0]  # первая — самая приоритетная
            parts.append(
                f"Главная задача: {top[1]}"
                + (f" (до {top[3]})" if top[3] else "")
            )
            if len(tasks) > 1:
                parts.append(f"Ещё задач в списке: {len(tasks) - 1}")

        if todays_reminders:
            parts.append(
                "Напоминания на сегодня: "
                + "; ".join(f"{r[1]} в {r[2][11:16]}" for r in todays_reminders[:2])
            )

        if moods:
            parts.append(f"Последнее настроение Сократа: {moods[0][0]}")

        if diary:
            parts.append(f"Вчера в дневнике: {diary[0][1][:150]}")

        if news:
            # Только первые 800 символов — не перегружаем
            parts.append(f"Свежее из tech-мира: {news[:800]}")

        user_content = "Напиши утреннее сообщение.\n\n" + "\n".join(parts)

        response = await self._llm.ainvoke([
            SystemMessage(content=_SYSTEM),
            HumanMessage(content=user_content),
        ])

        return AgentResult(
            success=True,
            content=str(response.content),
            agent_name=self.agent_name,
            needs_critic=False,
        )


def _day_ru(dt: datetime) -> str:
    days = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
    return days[dt.weekday()]


async def _get_weather() -> str:
    """Получает погоду через wttr.in."""
    try:
        import httpx
        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.get(_WEATHER_API)
            if r.status_code != 200:
                return ""
            data    = r.json()
            cur     = data["current_condition"][0]
            temp    = cur["temp_C"]
            feels   = cur["FeelsLikeC"]
            desc    = (
                cur["lang_ru"][0]["value"]
                if cur.get("lang_ru")
                else cur["weatherDesc"][0]["value"]
            )
            wind    = cur["windspeedKmph"]

            # Что надеть — простая эвристика
            temp_i = int(temp)
            if temp_i < 0:
                tip = "тепло оденься"
            elif temp_i < 10:
                tip = "куртка обязательна"
            elif temp_i < 18:
                tip = "возьми лёгкую куртку"
            else:
                tip = "можно налегке"

            return f"{desc}, {temp}°C (ощущается {feels}°C), ветер {wind} км/ч — {tip}"
    except Exception as e:
        logger.warning("Погода недоступна: %s", e)
        return ""
