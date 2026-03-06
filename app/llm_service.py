import logging
from langchain_groq import ChatGroq
from langchain_core.messages import HumanMessage, SystemMessage, BaseMessage

from app.config import settings
from app.database import load_history, save_messages

logger = logging.getLogger(__name__)


class LLMService:
    """Сервис для работы с языковой моделью."""

    def __init__(self) -> None:
        self._llm = ChatGroq(
            api_key=settings.groq_api_key,
            model=settings.model_name,
            temperature=settings.temperature,
        )

    async def chat(self, user_id: int, user_message: str) -> str:
        """Отправляет сообщение модели и возвращает ответ."""

        # Загружаем историю из БД при каждом запросе
        history = load_history(user_id, limit=settings.max_history)

        messages: list[BaseMessage] = [
            SystemMessage(content=settings.system_prompt),
            *history,
            HumanMessage(content=user_message),
        ]

        response = await self._llm.ainvoke(messages)
        reply = str(response.content)

        # Сохраняем в БД только после успешного ответа
        save_messages(user_id, user_message, reply)

        usage = getattr(response, "usage_metadata", None)
        logger.debug("user_id=%s | usage=%s", user_id, usage)

        return reply
