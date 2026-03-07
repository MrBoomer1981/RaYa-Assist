import base64
import logging
from groq import AsyncGroq

from app.config import settings

logger = logging.getLogger(__name__)

_VISION_MODEL = "llama-3.2-90b-vision-preview"
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

        try:
            response = await self._client.chat.completions.create(
                model=_VISION_MODEL,
                messages=[
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
                ],
                max_tokens=1024,
            )
            result = response.choices[0].message.content or ""
            logger.info("🖼️ Изображение проанализировано: %d символов", len(result))
            return result.strip()

        except Exception as e:
            logger.exception("Ошибка анализа: %s", str(e))
            return str(e)  # временно возвращаем текст ошибки боту


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
