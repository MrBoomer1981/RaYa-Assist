import asyncio
import json
import logging
from langchain_groq import ChatGroq
from langchain_core.messages import HumanMessage, SystemMessage, BaseMessage

from app.config import settings
from app.database import load_history, save_messages, load_memory, save_memory

logger = logging.getLogger(__name__)

# Запускаем извлечение фактов только каждые N сообщений — экономим API лимит
MEMORY_EXTRACTION_EVERY_N = 5

MEMORY_EXTRACTION_PROMPT = """\
Проанализируй сообщение пользователя и извлеки важные факты о нём.
Интересуют: имя, возраст, профессия, город, интересы, цели, важные детали жизни.

Сообщение: {message}

Верни ТОЛЬКО JSON-массив строк. Если фактов нет — верни [].
Пример: ["Зовут Алексей", "Работает программистом", "Живёт в Москве"]
Только JSON, без пояснений и markdown."""


class LLMService:
    """Сервис для работы с языковой моделью."""

    def __init__(self) -> None:
        self._llm = ChatGroq(
            api_key=settings.groq_api_key,
            model=settings.model_name,
            temperature=settings.temperature,
        )
        # Счётчик сообщений на пользователя для throttling извлечения фактов
        self._msg_counter: dict[int, int] = {}

    async def _extract_facts_background(self, user_id: int, message: str) -> None:
        """Извлекает факты из сообщения в фоне — не блокирует ответ."""
        try:
            prompt = MEMORY_EXTRACTION_PROMPT.format(message=message)
            response = await self._llm.ainvoke([HumanMessage(content=prompt)])
            text = response.content.strip().replace("```json", "").replace("```", "").strip()
            facts: list[str] = json.loads(text)
            if isinstance(facts, list) and facts:
                save_memory(user_id, facts)
        except Exception:
            logger.debug("Не удалось извлечь факты для user_id=%s", user_id)

    def _should_extract_facts(self, user_id: int) -> bool:
        """Проверяет нужно ли извлекать факты для этого сообщения."""
        count = self._msg_counter.get(user_id, 0) + 1
        self._msg_counter[user_id] = count
        return count % MEMORY_EXTRACTION_EVERY_N == 1  # 1-е, 6-е, 11-е...

    async def chat(self, user_id: int, user_message: str) -> str:
        """Отправляет сообщение модели и возвращает ответ."""

        # Загружаем историю и память параллельно
        history = load_history(user_id, limit=settings.max_history)
        memory_facts = load_memory(user_id)

        # Формируем системный промпт с долгосрочной памятью
        system = settings.system_prompt
        if memory_facts:
            facts_text = "\n".join(f"- {f}" for f in memory_facts)
            system += f"\n\nЧто ты знаешь об этом пользователе:\n{facts_text}"

        messages: list[BaseMessage] = [
            SystemMessage(content=system),
            *history,
            HumanMessage(content=user_message),
        ]

        # Основной запрос и фоновое извлечение фактов запускаем параллельно
        tasks: list[asyncio.Task] = [
            asyncio.create_task(self._llm.ainvoke(messages))
        ]
        if self._should_extract_facts(user_id):
            tasks.append(
                asyncio.create_task(
                    self._extract_facts_background(user_id, user_message)
                )
            )

        # Ждём только основной запрос — факты могут досчитываться в фоне
        response = await tasks[0]
        reply = str(response.content)

        # Сохраняем диалог
        save_messages(user_id, user_message, reply)

        usage = getattr(response, "usage_metadata", None)
        logger.debug("user_id=%s | usage=%s", user_id, usage)

        return reply
