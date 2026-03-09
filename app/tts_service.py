"""
tts_service.py — синтез речи через gTTS (Google Text-to-Speech).
Бесплатно, без API ключей, поддерживает русский язык.
"""
import io
import logging
import re

logger = logging.getLogger(__name__)

_MAX_CHARS = 800
_LANG      = "ru"


class TTSService:

    def __init__(self) -> None:
        try:
            from gtts import gTTS  # noqa: F401
            self._available = True
            logger.info("🔊 TTS сервис инициализирован (gTTS)")
        except ImportError:
            self._available = False
            logger.warning("⚠️ gTTS не установлен — TTS недоступен")

    @property
    def enabled(self) -> bool:
        return self._available

    async def synthesize(self, text: str) -> bytes | None:
        """Синтезирует речь. Возвращает MP3 байты или None."""
        if not self.enabled:
            return None

        clean = _clean_text(text)
        if len(clean) > _MAX_CHARS:
            clean = clean[:_MAX_CHARS] + "."
        if not clean.strip():
            return None

        try:
            import asyncio
            # gTTS синхронный — запускаем в executor чтобы не блокировать event loop
            loop = asyncio.get_event_loop()
            audio = await loop.run_in_executor(None, _synthesize_sync, clean)
            logger.info("🔊 TTS: синтезировано %d символов", len(clean))
            return audio
        except Exception:
            logger.exception("TTS: ошибка синтеза")
            return None


def _synthesize_sync(text: str) -> bytes:
    from gtts import gTTS
    buf = io.BytesIO()
    gTTS(text=text, lang=_LANG, slow=False).write_to_fp(buf)
    return buf.getvalue()


def _clean_text(text: str) -> str:
    """Убирает markdown перед озвучкой."""
    text = re.sub(r"\*\*(.+?)\*\*",   r"\1", text)
    text = re.sub(r"\*(.+?)\*",       r"\1", text)
    text = re.sub(r"`(.+?)`",         r"\1", text)
    text = re.sub(r"#{1,6}\s",        "",    text)
    text = re.sub(r"\[(.+?)\]\(.+?\)",r"\1", text)
    text = re.sub(r"[⏰🔴🟡🟢✅❌⚠️🔍🌅💬🧠📔🎙️🔊⏳⏹️]", "", text)
    text = re.sub(r"\n{3,}", "\n\n",  text)
    text = re.sub(r" {2,}",  " ",     text)
    return text.strip()
