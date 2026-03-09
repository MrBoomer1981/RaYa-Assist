"""
morning_agent.py — утренний дайджест.
Погода (Самара) + tech новости + задачи на день + рефлексия из дневника.
Запускается планировщиком в 08:00 по московскому времени (UTC+3 → 05:00 UTC).
"""
import logging
from datetime import datetime

from langchain_core.messages import HumanMessage, SystemMessage

from app.agents.base_agent import AgentContext, AgentResult, BaseAgent
from app.config import settings
from app.database import get_active_reminders, get_active_tasks, get_recent_moods, load_diary_entries

logger = logging.getLogger(__name__)

_CITY = "Самара, Россия"
_WEATHER_API = "https://wttr.in/Samara?format=j1&lang=ru"

_SYSTEM = """\
Ты RaYa — личный ассистент Сократа.
Сейчас ты готовишь утренний дайджест. Пиши живо, по-человечески, без сухих перечислений.

Структура дайджеста (строго):
1. Приветствие с учётом дня недели и времени года — короткое, тёплое
2. Погода в Самаре — главное одной фразой, что надеть/взять
3. Главные tech-новости — 2-3 темы которые реально важны, с твоим мнением
4. Задачи на сегодня — если есть, выдели самую важную
5. Напоминания на сегодня — если есть
6. Короткая мысль или вопрос от себя — что-то что заставит думать

Тон: как умный друг который знает тебя хорошо. Не доклад, а живой разговор.
Обращайся только "Сократ". Длина — не больше 400 слов."""


class MorningAgent(BaseAgent):
    agent_name = "morning"
    timeout = 45

    def _system_prompt(self) -> str:
        return _SYSTEM

    async def _execute(self, ctx: AgentContext) -> AgentResult:
        now = datetime.utcnow()

        # Собираем данные параллельно
        weather = await _get_weather()
        tasks   = get_active_tasks(ctx.user_id)
        reminders = get_active_reminders(ctx.user_id)
        moods   = get_recent_moods(ctx.user_id, limit=3)
        diary   = load_diary_entries(ctx.user_id, limit=2)

        # Фильтруем напоминания на сегодня
        today = now.strftime("%Y-%m-%d")
        todays_reminders = [r for r in reminders if r[2].startswith(today)]

        # Формируем контекст для модели
        context_parts = [f"Текущее время UTC: {now.strftime('%Y-%m-%d %H:%M')}"]

        if weather:
            context_parts.append(f"Погода в Самаре:\n{weather}")

        if tasks:
            priority_map = {1: "🔴 высокий", 2: "🟡 средний", 3: "🟢 низкий"}
            tasks_str = "\n".join(
                f"- [{priority_map.get(p, '?')}] {t}" + (f" (до {d})" if d else "")
                for _, t, p, d in tasks[:5]
            )
            context_parts.append(f"Активные задачи:\n{tasks_str}")

        if todays_reminders:
            rem_str = "\n".join(f"- {r[2]}: {r[1]}" for r in todays_reminders)
            context_parts.append(f"Напоминания на сегодня:\n{rem_str}")

        if moods:
            mood_str = ", ".join(f"{m[0]}" for m in moods)
            context_parts.append(f"Последнее настроение Сократа: {mood_str}")

        if diary:
            last_entry = diary[0][1][:200]
            context_parts.append(f"Последняя запись в дневнике: {last_entry}...")

        user_content = (
            "Подготовь утренний дайджест.\n\n"
            + "\n\n".join(context_parts)
            + "\n\nТех-новости найди сам через поиск — самое актуальное в AI и технологиях."
        )

        messages = [
            SystemMessage(content=_SYSTEM),
            HumanMessage(content=user_content),
        ]

        response = await self._llm.ainvoke(messages)

        return AgentResult(
            success=True,
            content=str(response.content),
            agent_name=self.agent_name,
            needs_critic=False,
        )


async def _get_weather() -> str:
    """Получает погоду через wttr.in API."""
    try:
        import httpx
        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.get(_WEATHER_API)
            if r.status_code != 200:
                return ""
            data = r.json()
            current = data["current_condition"][0]
            temp_c  = current["temp_C"]
            feels   = current["FeelsLikeC"]
            desc    = current["lang_ru"][0]["value"] if current.get("lang_ru") else current["weatherDesc"][0]["value"]
            wind    = current["windspeedKmph"]
            humidity = current["humidity"]

            # Завтра
            tomorrow = data["weather"][1] if len(data["weather"]) > 1 else None
            tomorrow_str = ""
            if tomorrow:
                t_max = tomorrow["maxtempC"]
                t_min = tomorrow["mintempC"]
                tomorrow_str = f" | Завтра: {t_min}..{t_max}°C"

            return (
                f"{desc}, {temp_c}°C (ощущается {feels}°C), "
                f"ветер {wind} км/ч, влажность {humidity}%{tomorrow_str}"
            )
    except Exception as e:
        logger.warning("Погода недоступна: %s", e)
        return ""
