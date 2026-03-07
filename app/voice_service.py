import logging
import tempfile
from pathlib import Path
from typing import Optional
from groq import AsyncGroq

from app.config import settings

logger = logging.getLogger(__name__)

_AUDIO_SUFFIX = ".ogg"
_WHISPER_MODEL = "whisper-large-v3-turbo"


class VoiceService:
    """Сервис для распознавания голосовых сообщений через Groq Whisper."""

    def __init__(self) -> None:
        self._client = AsyncGroq(api_key=settings.groq_api_key)

    async def transcribe(self, audio_bytes: bytes) -> str:
        """
        Принимает аудио байты (OGG/Opus от Telegram).
        Возвращает распознанный текст или пустую строку при ошибке.
        """
        if not audio_bytes:
            logger.warning("transcribe вызван с пустыми байтами")
            return ""

        tmp_path: Optional[Path] = None
        try:
            with tempfile.NamedTemporaryFile(
                suffix=_AUDIO_SUFFIX, delete=False
            ) as tmp:
                tmp.write(audio_bytes)
                tmp_path = Path(tmp.name)

            with open(tmp_path, "rb") as audio_file:
                transcription = await self._client.audio.transcriptions.create(
                    file=(tmp_path.name, audio_file),
                    model=_WHISPER_MODEL,
                    response_format="text",
                )

            text = str(transcription).strip()
            if text:
                logger.info("🎤 Распознано %d символов", len(text))
            else:
                logger.warning("Whisper вернул пустой текст")
            return text

        except Exception:
            logger.exception("Ошибка распознавания голоса")
            return ""
        finally:
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)
