"""
llm_service.py — сервис обработки сообщений.
Делегирует оркестратору, сохраняет историю, извлекает факты в фоне.
"""
import asyncio
import json
import logging
from collections.abc import Coroutine
from dataclasses import dataclass, field
from typing import Any, Optional

from langchain_core.messages import BaseMessage, HumanMessage
from langchain_groq import ChatGroq

from app.config import settings
from app.database import load_memory, save_memory, save_messages

logger = logging.getLogger(__name__)

_MEMORY_EXTRACTION_EVERY_N = 5

_SEARCH_KEYWORDS: tuple[str, ...] = (
    "новост", "сейчас", "сегодня", "вчера", "курс", "цена", "погод",
    "актуальн", "последн", "недавно", "2024", "2025", "2026",
    "что происходит", "найди", "поищи", "узнай",
)

_MEMORY_EXTRACTION_PROMPT = """\
Проанализируй сообщение пользователя и извлеки важные факты о нём.
Интересуют: имя, возраст, профессия, город, интересы, цели, важные детали жизни.

Сообщение: {message}

Верни ТОЛЬКО JSON-массив строк. Если фактов нет — верни [].
Пример: ["Зовут Алексей", "Работает программистом", "Живёт в Москве"]
Только JSON, без пояснений и markdown."""


@dataclass
class ChatResult:
    """Результат одного обращения к модели."""
    reply: str
    reminder: Optional[dict]  = field(default=None)
    agent_name: str           = "raya"
    metadata: dict            = field(default_factory=dict)


class LLMService:
    """
    Сервис для обработки сообщений.
    Делегирует оркестратору — не содержит бизнес-логики агентов.
    """

    def __init__(self) -> None:
        self._llm = ChatGroq(
            api_key=settings.groq_api_key,
            model=settings.model_name,
            temperature=settings.temperature,
        )
        self._search: Optional[Any] = None
        if settings.search_enabled:
            from app.search_service import SearchService
            self._search = SearchService()
            logger.info("🔍 Поиск в интернете включён")

        self._msg_counter: dict[int, int] = {}
        self._background_tasks: set[asyncio.Task[None]] = set()
        self._orchestrator: Optional[Any] = None

    # ── Вспомогательные ───────────────────────────────────────────────────────

    def _run_background(self, coro: Coroutine[Any, Any, None]) -> None:
        """Запускает корутину в фоне, защищая задачу от GC."""
        task: asyncio.Task[None] = asyncio.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    def _needs_search(self, message: str) -> bool:
        if not self._search:
            return False
        msg_lower = message.lower()
        return any(kw in msg_lower for kw in _SEARCH_KEYWORDS)

    def _should_extract_facts(self, user_id: int) -> bool:
        count = self._msg_counter.get(user_id, 0) + 1
        self._msg_counter[user_id] = count
        return count % _MEMORY_EXTRACTION_EVERY_N == 1

    def _get_orchestrator(self):
        """Ленивая инициализация оркестратора."""
        if self._orchestrator is None:
            from app.agents.orchestrator import Orchestrator
            self._orchestrator = Orchestrator()
        return self._orchestrator

    # ── Фоновые задачи ────────────────────────────────────────────────────────

    async def _extract_facts_background(self, user_id: int, message: str) -> None:
        try:
            prompt = _MEMORY_EXTRACTION_PROMPT.format(message=message)
            response = await self._llm.ainvoke([HumanMessage(content=prompt)])
            text = (
                str(response.content)
                .strip()
                .replace("```json", "")
                .replace("```", "")
                .strip()
            )
            facts: list[str] = json.loads(text)
            if isinstance(facts, list) and facts:
                save_memory(user_id, facts)
        except Exception:
            logger.debug("Не удалось извлечь факты для user_id=%s", user_id)

    # ── Основной метод ────────────────────────────────────────────────────────

    async def chat(self, user_id: int, user_message: str) -> ChatResult:
        """
        Точка входа — делегирует оркестратору.
        Сохраняет в историю, извлекает факты в фоне.
        """
        # Поиск параллельно пока роутер думает
        search_task: Optional[asyncio.Task[str]] = None
        if self._needs_search(user_message) and self._search is not None:
            search_task = asyncio.create_task(self._search.search(user_message))

        search_results = ""
        if search_task is not None:
            try:
                search_results = await search_task
                if search_results:
                    logger.info("user_id=%s | поиск добавлен в контекст", user_id)
            except Exception:
                logger.exception("user_id=%s | ошибка поиска", user_id)
                search_task.cancel()

        agent_result = await self._get_orchestrator().run(
            user_id=user_id,
            message=user_message,
            search_results=search_results,
        )

        reply    = agent_result.content
        reminder = (agent_result.metadata or {}).get("reminder")

        save_messages(user_id, user_message, reply)

        if self._should_extract_facts(user_id):
            self._run_background(
                self._extract_facts_background(user_id, user_message)
            )

        logger.debug(
            "user_id=%s | агент=%s | reminder=%s",
            user_id, agent_result.agent_name, reminder is not None,
        )
        return ChatResult(
            reply=reply,
            reminder=reminder,
            agent_name=agent_result.agent_name,
            metadata=agent_result.metadata or {},
        )

    # ── Вспомогательные для других обработчиков ───────────────────────────────

    def save_photo_exchange(
        self, user_id: int, user_note: str, vision_result: str
    ) -> None:
        """Сохраняет фото-обмен в историю. Синхронный — без await."""
        save_messages(user_id, user_note, vision_result)

    async def chat_with_document(
        self,
        user_id: int,
        doc_text: str,
        user_question: str,
        doc_name: str = "документ",
    ) -> str:
        """Отвечает на вопрос по содержимому документа."""
        from langchain_core.messages import HumanMessage, SystemMessage
        from app.database import load_memory

        memory_facts = load_memory(user_id)
        system = settings.system_prompt
        if memory_facts:
            facts = "\n".join(f"- {f}" for f in memory_facts)
            system = f"{system}\n\nЧто известно о пользователе:\n{facts}"

        question = user_question.strip() or "Кратко изложи содержание документа."
        combined = (
            f"Вот содержимое документа «{doc_name}»:\n\n"
            f"{doc_text}\n\n"
            f"Вопрос: {question}"
        )

        messages: list[BaseMessage] = [
            SystemMessage(content=system),
            HumanMessage(content=combined),
        ]

        response = await self._llm.ainvoke(messages)
        reply = str(response.content)

        save_messages(user_id, f"[Документ: {doc_name}] {question}", reply)
        logger.debug("user_id=%s | документ: %s", user_id, doc_name)
        return reply
