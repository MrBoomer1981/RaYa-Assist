"""
raya_agent.py — главный агент RaYa.
Используется как fallback и для общих разговоров.
Единственный агент у которого есть доступ к напоминаниям.
"""
import logging
from datetime import datetime

from app.agents.base_agent import AgentContext, AgentResult, BaseAgent
from app.config import settings

logger = logging.getLogger(__name__)


def _build_raya_system(now_utc: datetime) -> str:
    """Системный промпт RaYa с временем и инструкцией по напоминаниям."""
    from datetime import timedelta
    ex_5min     = (now_utc + timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
    ex_1h       = (now_utc + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    ex_tomorrow = (now_utc + timedelta(days=1)).strftime("%Y-%m-%d") + " 09:00:00"

    reminder_block = f"""

--- НАПОМИНАНИЯ ---
Текущее время UTC: {now_utc.strftime("%Y-%m-%d %H:%M:%S")}

Если пользователь просит напомнить — включи в ответ:
<reminder>{{"text": "текст", "remind_at": "YYYY-MM-DD HH:MM:SS"}}</reminder>

Примеры:
- "через 5 минут" → {ex_5min}
- "через час"     → {ex_1h}
- "завтра в 9"    → {ex_tomorrow}

Если напоминания нет — НЕ включай тег.
--- КОНЕЦ ---"""

    return settings.system_prompt + reminder_block


class RayaAgent(BaseAgent):
    agent_name = "raya"
    timeout = 30

    def _system_prompt(self) -> str:
        # Базовый промпт — время добавляется динамически в _execute
        return settings.system_prompt

    async def _execute(self, ctx: AgentContext) -> AgentResult:
        from langchain_core.messages import SystemMessage, HumanMessage

        now_utc = datetime.utcnow()
        system  = _build_raya_system(now_utc)

        # Собираем сообщения вручную — нужен динамический системный промпт
        from app.agents.base_agent import BaseAgent
        import re

        content = ctx.message
        if ctx.search_results:
            content = (
                f"{ctx.message}\n\n"
                f"[Контекст из поиска:]\n{ctx.search_results}"
            )

        if ctx.memory_facts:
            facts = "\n".join(f"- {f}" for f in ctx.memory_facts)
            system = f"{system}\n\nЧто известно о пользователе:\n{facts}"

        from langchain_core.messages import SystemMessage, HumanMessage
        messages = [
            SystemMessage(content=system),
            *ctx.history,
            HumanMessage(content=content),
        ]

        response = await self._llm.ainvoke(messages)
        raw = str(response.content)

        # Парсим напоминание если есть
        reminder = _parse_reminder(raw, now_utc)
        reply    = _clean_reply(raw)

        return AgentResult(
            success=True,
            content=reply,
            agent_name=self.agent_name,
            needs_critic=False,
            metadata={"reminder": reminder},
        )


def _parse_reminder(raw: str, now_utc: datetime) -> dict | None:
    """Извлекает JSON напоминания из тега <reminder>."""
    import json, re
    match = re.search(r"<reminder>(.*?)</reminder>", raw, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(1).strip())
        if not isinstance(data, dict):
            return None
        if "text" not in data or "remind_at" not in data:
            return None
        remind_str = str(data["remind_at"]).strip()
        if len(remind_str) == 16:
            remind_str += ":00"
        data["remind_at"] = remind_str
        remind_dt = datetime.strptime(remind_str, "%Y-%m-%d %H:%M:%S")
        if remind_dt <= now_utc:
            return None
        logger.info("⏰ RaYa: напоминание '%s' на %s UTC", data["text"], remind_str)
        return data
    except Exception:
        return None


def _clean_reply(raw: str) -> str:
    """Убирает тег <reminder> из текста ответа."""
    import re
    return re.sub(r"\s*<reminder>.*?</reminder>", "", raw, flags=re.DOTALL).strip()
