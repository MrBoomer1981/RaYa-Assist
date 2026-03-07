import base64
import logging
from groq import AsyncGroq

from app.config import settings

logger = logging.getLogger(__name__)

_VISION_MODELS = [
    "meta-llama/llama-4-scout-17b-16e-instruct",  # Llama 4 — актуальная
    "meta-llama/llama-4-maverick-17b-128e-instruct",  # Резервная
]
_MAX_IMAGE_BYTES = 20 * 1024 * 1024  # 20 МБ — лимит Groq
_DEFAULT_PROMPT = "Опиши подробно что ты видишь на этом изображении."


class VisionService:
    """Сервис для анализа изображений через Groq Vision."""

    def __init__(self) -> None:
        self._client = AsyncGroq(api_key=settings.groq_api_key)

    async def analyze(self, image_bytes: bytes, user_prompt: str = "") -> str:
        """
        Анализирует изображение и возвращает текстовый ответ.
        user_prompt — вопрос пользователя про фото (опционально).
        Возвращает пустую строку при ошибке.
        """
        if not image_bytes:
            logger.warning("analyze вызван с пустыми байтами")
            return ""

        if len(image_bytes) > _MAX_IMAGE_BYTES:
            logger.warning("Изображение слишком большое: %d байт", len(image_bytes))
            return ""

        # Определяем формат по сигнатуре байтов
        media_type = _detect_media_type(image_bytes)
        image_b64 = base64.standard_b64encode(image_bytes).decode("utf-8")
        prompt = user_prompt.strip() or _DEFAULT_PROMPT

        messages_payload = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{media_type};base64,{image_b64}"
                        },
                    },
                    {
                        "type": "text",
                        "text": prompt,
                    },
                ],
            }
        ]

        # Пробуем модели по очереди — fallback при пустом ответе или ошибке
        last_error: str = ""
        for model in _VISION_MODELS:
            try:
                logger.info("🖼️ Пробуем модель: %s", model)
                response = await self._client.chat.completions.create(
                    model=model,
                    messages=messages_payload,
                    max_tokens=1024,
                )
                result = (response.choices[0].message.content or "").strip()

                if result:
                    logger.info(
                        "✅ Модель %s вернула %d символов", model, len(result)
                    )
                    return result
                else:
                    logger.warning("⚠️ Модель %s вернула пустой ответ", model)
                    last_error = f"Модель {model} вернула пустой ответ"

            except Exception as e:
                logger.warning("❌ Модель %s: %s", model, str(e))
                last_error = str(e)

        logger.error("Все vision модели недоступны. Последняя ошибка: %s", last_error)
        return ""


def _detect_media_type(data: bytes) -> str:
    """Определяет MIME-тип изображения по сигнатуре байтов."""
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    # По умолчанию — JPEG (самый частый формат от Telegram)
    return "image/jpeg"
