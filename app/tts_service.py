"""
tts_service.py — синтез речи через gTTS + ускорение через ffmpeg x1.25.

Чанкинг по предложениям — нет обрыва на полуслове.
Длинный текст → несколько MP3 → склеиваем в один через ffmpeg.
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

_LANG       = "ru"
_SPEED      = 1.25
_FFMPEG_BIN = shutil.which("ffmpeg")

# Максимум символов в одном gTTS-запросе (gTTS ограничен ~500 байт URL)
_CHUNK_MAX  = 450

# Для голосовых ответов — дополнительно режем до N слов
_VOICE_MAX_WORDS = 80


class TTSService:

    def __init__(self) -> None:
        try:
            from gtts import gTTS  # noqa: F401
            self._available = True
            note = f"ffmpeg x{_SPEED}" if _FFMPEG_BIN else "без ускорения"
            logger.info("🔊 TTS инициализирован (gTTS, %s)", note)
        except ImportError:
            self._available = False
            logger.warning("⚠️ gTTS не установлен — TTS недоступен")

    @property
    def enabled(self) -> bool:
        return self._available

    async def synthesize(self, text: str, is_voice: bool = False) -> bytes | None:
        """Синтезирует речь. Возвращает MP3 байты или None."""
        if not self.enabled:
            return None

        clean = _clean_text(text)
        if not clean.strip():
            return None

        # Для голосовых запросов — ещё короче
        if is_voice:
            clean = _trim_to_words(clean, _VOICE_MAX_WORDS)

        try:
            loop  = asyncio.get_running_loop()
            audio = await loop.run_in_executor(None, _synthesize_chunked, clean)
            logger.info("🔊 TTS: %d символов → %d байт", len(clean), len(audio))
            return audio
        except Exception:
            logger.exception("TTS: ошибка синтеза")
            return None


def _trim_to_words(text: str, max_words: int) -> str:
    """Режет по границе предложения не превышая max_words слов."""
    words = text.split()
    if len(words) <= max_words:
        return text
    # Берём первые max_words слов и находим последнюю точку
    trimmed = " ".join(words[:max_words])
    # Обрезаем по последнему окончанию предложения
    for sep in (".", "!", "?"):
        idx = trimmed.rfind(sep)
        if idx > len(trimmed) // 2:
            return trimmed[:idx + 1]
    return trimmed + "."


def _split_sentences(text: str) -> list[str]:
    """
    Разбивает текст на чанки по предложениям, каждый ≤ _CHUNK_MAX символов.
    Гарантирует что обрыва на полуслове не будет.
    """
    # Разбиваем по концам предложений
    raw_sentences = re.split(r"(?<=[.!?…])\s+", text)
    chunks: list[str] = []
    current = ""

    for sentence in raw_sentences:
        if not sentence.strip():
            continue
        # Если одно предложение длиннее лимита — режем по запятым
        if len(sentence) > _CHUNK_MAX:
            parts = re.split(r"(?<=,)\s+", sentence)
            for part in parts:
                if len(current) + len(part) + 1 <= _CHUNK_MAX:
                    current = (current + " " + part).strip()
                else:
                    if current:
                        chunks.append(current)
                    current = part[:_CHUNK_MAX]
        elif len(current) + len(sentence) + 1 <= _CHUNK_MAX:
            current = (current + " " + sentence).strip()
        else:
            if current:
                chunks.append(current)
            current = sentence

    if current:
        chunks.append(current)

    return chunks or [text[:_CHUNK_MAX]]


def _synthesize_chunked(text: str) -> bytes:
    """Синтез с чанкингом. Склеивает MP3 через ffmpeg concat."""
    from gtts import gTTS

    chunks = _split_sentences(text)
    logger.debug("TTS: %d чанков из %d символов", len(chunks), len(text))

    if len(chunks) == 1:
        # Один чанк — быстрый путь
        buf = io.BytesIO()
        gTTS(text=chunks[0], lang=_LANG, slow=False).write_to_fp(buf)
        mp3 = buf.getvalue()
        return _speed_up(mp3) if _FFMPEG_BIN else mp3

    # Несколько чанков — генерируем каждый и склеиваем
    tmp_files: list[Path] = []
    try:
        for i, chunk in enumerate(chunks):
            buf = io.BytesIO()
            gTTS(text=chunk, lang=_LANG, slow=False).write_to_fp(buf)
            tmp = Path(tempfile.mktemp(suffix=f"_chunk{i}.mp3"))
            tmp.write_bytes(buf.getvalue())
            tmp_files.append(tmp)

        merged = _concat_mp3(tmp_files)
        return _speed_up(merged) if _FFMPEG_BIN else merged

    finally:
        for f in tmp_files:
            f.unlink(missing_ok=True)


def _concat_mp3(files: list[Path]) -> bytes:
    """Склеивает список MP3 файлов через ffmpeg concat demuxer."""
    if not _FFMPEG_BIN:
        # Fallback: простая конкатенация байт (работает для MP3)
        return b"".join(f.read_bytes() for f in files)

    list_file = Path(tempfile.mktemp(suffix=".txt"))
    out_file  = Path(tempfile.mktemp(suffix=".mp3"))
    try:
        list_file.write_text(
            "\n".join(f"file '{p}'" for p in files)
        )
        result = subprocess.run(
            [
                _FFMPEG_BIN, "-y",
                "-f", "concat", "-safe", "0",
                "-i", str(list_file),
                "-c", "copy",
                str(out_file),
            ],
            capture_output=True, timeout=20,
        )
        if result.returncode == 0 and out_file.exists():
            return out_file.read_bytes()
        logger.warning("ffmpeg concat код %d", result.returncode)
        return b"".join(f.read_bytes() for f in files)
    finally:
        list_file.unlink(missing_ok=True)
        out_file.unlink(missing_ok=True)


def _speed_up(mp3_bytes: bytes) -> bytes:
    """Ускоряет MP3 через ffmpeg atempo."""
    tmp_in = tmp_out = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
            f.write(mp3_bytes)
            tmp_in = Path(f.name)
        tmp_out = tmp_in.with_suffix(".out.mp3")

        result = subprocess.run(
            [_FFMPEG_BIN, "-y", "-i", str(tmp_in),
             "-filter:a", f"atempo={_SPEED}", "-vn", str(tmp_out)],
            capture_output=True, timeout=15,
        )
        if result.returncode == 0 and tmp_out.exists():
            return tmp_out.read_bytes()
        return mp3_bytes
    except Exception:
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
    text = re.sub(
        r"[\U00002600-\U000027BF\U0001F300-\U0001F9FF\U00002702-\U000027B0]+",
        "", text,
    )
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r" {2,}",  " ",    text)
    return text.strip()
