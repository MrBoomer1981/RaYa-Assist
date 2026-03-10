"""
tts_service.py — синтез речи через gTTS + ускорение через ffmpeg.

gTTS генерирует MP3, ffmpeg ускоряет до x1.25 — звучит естественно.
Если ffmpeg недоступен — отдаём оригинальную скорость.
"""
import asyncio
import io
import logging
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

_MAX_CHARS   = 800
_LANG        = "ru"
_SPEED       = 1.25          # коэффициент ускорения
_FFMPEG_BIN  = shutil.which("ffmpeg")   # None если не установлен


class TTSService:

    def __init__(self) -> None:
        try:
            from gtts import gTTS  # noqa: F401
            self._available = True
            speed_note = f"ffmpeg x{_SPEED}" if _FFMPEG_BIN else "без ускорения (ffmpeg не найден)"
            logger.info("🔊 TTS инициализирован (gTTS, %s)", speed_note)
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
            loop  = asyncio.get_running_loop()
            audio = await loop.run_in_executor(None, _synthesize_sync, clean)
            logger.info("🔊 TTS: %d символов → %d байт", len(clean), len(audio))
            return audio
        except Exception:
            logger.exception("TTS: ошибка синтеза")
            return None


def _synthesize_sync(text: str) -> bytes:
    """Синтез + ускорение через ffmpeg (синхронный, запускается в executor)."""
    # Шаг 1 — gTTS → MP3 в памяти
    from gtts import gTTS
    buf = io.BytesIO()
    gTTS(text=text, lang=_LANG, slow=False).write_to_fp(buf)
    mp3_bytes = buf.getvalue()

    # Шаг 2 — ffmpeg ускоряет, если доступен
    if _FFMPEG_BIN:
        mp3_bytes = _speed_up(mp3_bytes)

    return mp3_bytes


def _speed_up(mp3_bytes: bytes) -> bytes:
    """
    Ускоряет MP3 через ffmpeg atempo фильтр.
    atempo принимает значения 0.5–2.0 — для x1.25 один фильтр.
    Возвращает ускоренные байты или оригинал при ошибке.
    """
    tmp_in  = None
    tmp_out = None
    try:
        # Пишем входной файл
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
            f.write(mp3_bytes)
            tmp_in = Path(f.name)

        tmp_out = tmp_in.with_suffix(".out.mp3")

        result = subprocess.run(
            [
                _FFMPEG_BIN,
                "-y",                        # перезаписать если есть
                "-i", str(tmp_in),           # входной файл
                "-filter:a", f"atempo={_SPEED}",  # ускорение
                "-vn",                       # без видео
                str(tmp_out),
            ],
            capture_output=True,
            timeout=15,
        )

        if result.returncode == 0 and tmp_out.exists():
            fast_bytes = tmp_out.read_bytes()
            logger.debug("🔊 ffmpeg x%.2f: %d → %d байт", _SPEED, len(mp3_bytes), len(fast_bytes))
            return fast_bytes

        logger.warning("ffmpeg вернул код %d: %s", result.returncode, result.stderr[:200])
        return mp3_bytes

    except subprocess.TimeoutExpired:
        logger.warning("ffmpeg: timeout")
        return mp3_bytes
    except Exception:
        logger.exception("ffmpeg: ошибка ускорения")
        return mp3_bytes
    finally:
        if tmp_in  and tmp_in.exists():  tmp_in.unlink(missing_ok=True)
        if tmp_out and tmp_out.exists(): tmp_out.unlink(missing_ok=True)


def _clean_text(text: str) -> str:
    """Убирает markdown и эмодзи перед озвучкой."""
    text = re.sub(r"\*\*(.+?)\*\*",    r"\1", text)
    text = re.sub(r"\*(.+?)\*",        r"\1", text)
    text = re.sub(r"`(.+?)`",          r"\1", text)
    text = re.sub(r"#{1,6}\s",         "",    text)
    text = re.sub(r"\[(.+?)\]\(.+?\)", r"\1", text)
    # Убираем все эмодзи через unicode диапазон
    text = re.sub(
        r"[\U00002600-\U000027BF"
        r"\U0001F300-\U0001F9FF"
        r"\U00002702-\U000027B0]+",
        "", text,
    )
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r" {2,}",  " ",    text)
    return text.strip()
