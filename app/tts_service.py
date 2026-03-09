"""
tts_service.py — синтез речи через ElevenLabs API.
Бесплатный tier: 10,000 символов/месяц.
Голос: Rachel (en) или Bella — лучшие для русского на бесплатном tier.
"""
import logging
import os

import httpx

logger = logging.getLogger(__name__)

# Бесплатные голоса ElevenLabs (не требуют платного tier)
_VOICE_ID    = "21m00Tcm4TlvDq8ikWAM"  # Rachel — чистый, нейтральный
_API_URL     = f"https://api.elevenlabs.io/v1/text-to-speech/{_VOICE_ID}"
_MODEL_ID    = "eleven_multilingual_v2"  # Поддерживает русский
_MAX_CHARS   = 800   # Ограничиваем длину чтобы не тратить квоту


class TTSService:
    """Синтез речи через ElevenLabs."""

    def __init__(self) -> None:
        self._api_key = os.getenv("ELEVENLABS_API_KEY", "")
        if self._api_key:
            logger.info("🔊 TTS сервис инициализирован (ElevenLabs)")
        else:
            logger.warning("⚠️ ELEVENLABS_API_KEY не задан — TTS недоступен")

    @property
    def enabled(self) -> bool:
        return bool(self._api_key)

    async def synthesize(self, text: str) -> bytes | None:
        """
        Синтезирует речь из текста.
        Возвращает MP3 байты или None при ошибке.
        """
        if not self.enabled:
            return None

        # Обрезаем длинные ответы — экономим квоту
        clean = _clean_text(text)
        if len(clean) > _MAX_CHARS:
            clean = clean[:_MAX_CHARS] + "..."

        if not clean.strip():
            return None

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.post(
                    _API_URL,
                    headers={
                        "xi-api-key":   self._api_key,
                        "Content-Type": "application/json",
                    },
                    json={
                        "text":       clean,
                        "model_id":   _MODEL_ID,
                        "voice_settings": {
                            "stability":        0.5,
                            "similarity_boost": 0.75,
                        },
                    },
                )

                if response.status_code == 200:
                    logger.info("🔊 TTS: синтезировано %d символов", len(clean))
                    return response.content
                else:
                    logger.warning(
                        "TTS ошибка %d: %s",
                        response.status_code,
                        response.text[:200],
                    )
                    return None

        except Exception:
            logger.exception("TTS: ошибка запроса")
            return None


def _clean_text(text: str) -> str:
    """
    Убирает markdown и спецсимволы перед озвучкой.
    Модель возвращает **bold**, *italic*, `code` — они звучат плохо.
    """
    import re
    # Убираем markdown
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)   # **bold**
    text = re.sub(r"\*(.+?)\*",     r"\1", text)   # *italic*
    text = re.sub(r"`(.+?)`",       r"\1", text)   # `code`
    text = re.sub(r"#{1,6}\s",      "",    text)   # ### заголовки
    text = re.sub(r"\[(.+?)\]\(.+?\)", r"\1", text) # [ссылки](url)
    # Убираем эмодзи которые плохо звучат
    text = re.sub(r"[⏰🔴🟡🟢✅❌⚠️🔍🌅💬🧠📔]", "", text)
    # Нормализуем пробелы
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r" {2,}", " ", text)
    return text.strip()
