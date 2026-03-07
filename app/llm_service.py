import asyncio
import json
import logging
from collections.abc import Coroutine
from typing import Any, Optional

from langchain_groq import ChatGroq
from langchain_core.messages import HumanMessage, SystemMessage, BaseMessage

from app.config import settings
from app.database import load_history, save_messages, load_memory, save_memory

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


class LLMService:
    """Сервис для общения с языковой моделью Groq."""

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

    # ── Вспомогательные методы ────────────────────────────────────────────────

    def _run_background(self, coro: Coroutine[Any, Any, None]) -> None:
        """Запускает корутину в фоне, защищая задачу от сборщика мусора."""
        task: asyncio.Task[None] = asyncio.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    def _needs_search(self, message: str) -> bool:
        """Проверяет нужен ли поиск по ключевым словам."""
        if not self._search:
            return False
        msg_lower = message.lower()
        return any(kw in msg_lower for kw in _SEARCH_KEYWORDS)

    def _should_extract_facts(self, user_id: int) -> bool:
        """Возвращает True для каждого N-го сообщения пользователя."""
        count = self._msg_counter.get(user_id, 0) + 1
        self._msg_counter[user_id] = count
        return count % _MEMORY_EXTRACTION_EVERY_N == 1

    def _build_system_prompt(self, memory_facts: list[str]) -> str:
        """Формирует системный промпт с фактами о пользователе."""
        if not memory_facts:
            return settings.system_prompt
        facts_text = "\n".join(f"- {f}" for f in memory_facts)
        return (
            f"{settings.system_prompt}\n\n"
            f"Что ты знаешь об этом пользователе:\n{facts_text}"
        )

    # ── Фоновые задачи ────────────────────────────────────────────────────────

    async def _extract_facts_background(self, user_id: int, message: str) -> None:
        """Извлекает и сохраняет факты о пользователе из его сообщения."""
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

    async def chat(self, user_id: int, user_message: str) -> str:
        """Обрабатывает сообщение и возвращает ответ модели."""

        history = load_history(user_id, limit=settings.max_history)
        memory_facts = load_memory(user_id)

        # Поиск запускаем параллельно пока готовим промпт
        search_task: Optional[asyncio.Task[str]] = None
        if self._needs_search(user_message) and self._search is not None:
            search_task = asyncio.create_task(
                self._search.search(user_message)
            )

        system = self._build_system_prompt(memory_facts)

        # Ждём результат поиска
        final_message = user_message
        if search_task is not None:
            try:
                search_results = await search_task
                if search_results:
                    final_message = (
                        f"{user_message}\n\n"
                        f"[Контекст из поиска — используй для ответа:]\n"
                        f"{search_results}"
                    )
                    logger.info("user_id=%s | поиск добавлен в контекст", user_id)
            except Exception:
                logger.exception("user_id=%s | ошибка поиска", user_id)
                search_task.cancel()

        messages: list[BaseMessage] = [
            SystemMessage(content=system),
            *history,
            HumanMessage(content=final_message),
        ]

        response = await self._llm.ainvoke(messages)
        reply = str(response.content)

        save_messages(user_id, user_message, reply)

        if self._should_extract_facts(user_id):
            self._run_background(
                self._extract_facts_background(user_id, user_message)
            )

        logger.debug(
            "user_id=%s | usage=%s",
            user_id,
            getattr(response, "usage_metadata", None),
        )
        return reply

    def save_photo_exchange(
        self, user_id: int, user_note: str, vision_result: str
    ) -> None:
        """
        Сохраняет фото-обмен в историю разговора.
        Вызывается из main.py после успешного анализа изображения.
        """
        save_messages(user_id, user_note, vision_result)
