"""
raya_agent.py — главный агент RaYa.
Fallback для общих разговоров и единственный агент с напоминаниями.
"""
import logging
from datetime import datetime

from langchain_core.messages import HumanMessage, SystemMessage

from app.agents.base_agent import AgentContext, AgentResult, BaseAgent
from app.config import settings
from app.utils import build_reminder_prompt_block, clean_reminder_tag, parse_reminder

logger = logging.getLogger(__name__)


class RayaAgent(BaseAgent):
    agent_name = "raya"
    timeout = 30

    def _system_prompt(self) -> str:
        return settings.system_prompt

    async def _execute(self, ctx: AgentContext) -> AgentResult:
        now_utc = datetime.utcnow()

        # Динамический системный промпт с временем и инструкцией по напоминаниям
        system = settings.system_prompt + build_reminder_prompt_block(now_utc)

        if ctx.memory_facts:
            facts = "\n".join(f"- {f}" for f in ctx.memory_facts)
            system = f"{system}\n\nЧто известно о пользователе:\n{facts}"

        content = ctx.message
        if ctx.search_results:
            content = f"{ctx.message}\n\n[Контекст из поиска:]\n{ctx.search_results}"

        messages = [
            SystemMessage(content=system),
            *ctx.history,
            HumanMessage(content=content),
        ]

        response = await self._llm.ainvoke(messages)
        raw = str(response.content)

        reminder = parse_reminder(raw, now_utc)
        reply    = clean_reminder_tag(raw)

        return AgentResult(
            success=True,
            content=reply,
            agent_name=self.agent_name,
            needs_critic=False,
            metadata={"reminder": reminder},
        )
