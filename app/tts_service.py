"""
tts_service.py — синтез речи через gTTS + ускорение через ffmpeg.

Чанкинг по предложениям (≤80 символов = ≤480 байт URL — в пределах лимита gTTS).
Чанки генерируются ПАРАЛЛЕЛЬНО через ThreadPoolExecutor — нет зависания на одном.
Склейка через ffmpeg concat.
"""
import asyncio
import concurrent.futures
import io
import logging
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

_LANG            = "ru"
_SPEED           = 1.7          # скорость речи (atempo ffmpeg)
_FFMPEG_BIN      = shutil.which("ffmpeg")
_CHUNK_MAX       = 75           # символов на чанк (≤450 байт URL для кириллицы)
_VOICE_MAX_WORDS = 40           # слов для голосовых ответов
_EXECUTOR        = concurrent.futures.ThreadPoolExecutor(max_workers=4)


class TTSService:

    def __init__(self) -> None:
        try:
            from gtts import gTTS  # noqa: F401
            self._available = True
            note = f"ffmpeg x{_SPEED}" if _FFMPEG_BIN else "без ускорения (нет ffmpeg)"
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

        # Для голосовых — дополнительно обрезаем по словам
        if is_voice:
            clean = _trim_to_words(clean, _VOICE_MAX_WORDS)

        try:
            loop  = asyncio.get_running_loop()
            audio = await loop.run_in_executor(_EXECUTOR, _synthesize_chunked, clean)
            if audio:
                logger.info("🔊 TTS: %d симв → %d байт", len(clean), len(audio))
            return audio
        except Exception:
            logger.exception("TTS: ошибка синтеза")
            return None


# ── Синтез ────────────────────────────────────────────────────────────────────

def _synthesize_chunked(text: str) -> bytes | None:
    """Разбивает на чанки, генерирует параллельно, склеивает."""
    chunks = _split_sentences(text)
    logger.debug("TTS: %d чанков из %d символов", len(chunks), len(text))

    if len(chunks) == 1:
        mp3 = _gtts_single(chunks[0])
        return _speed_up(mp3) if (_FFMPEG_BIN and mp3) else mp3

    # Параллельная генерация чанков
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(_gtts_single, c) for c in chunks]
        results = []
        for i, f in enumerate(futures):
            try:
                data = f.result(timeout=15)
                if data:
                    results.append(data)
                else:
                    logger.warning("TTS: чанк %d вернул пустой результат", i)
            except Exception as e:
                logger.warning("TTS: чанк %d упал: %s", i, e)

    if not results:
        return None

    if len(results) == 1:
        return _speed_up(results[0]) if _FFMPEG_BIN else results[0]

    # Записываем чанки во временные файлы и склеиваем
    tmp_files: list[Path] = []
    try:
        for i, data in enumerate(results):
            tmp = Path(tempfile.mktemp(suffix=f"_c{i}.mp3"))
            tmp.write_bytes(data)
            tmp_files.append(tmp)

        merged = _concat_mp3(tmp_files)
        return _speed_up(merged) if (_FFMPEG_BIN and merged) else merged
    finally:
        for f in tmp_files:
            f.unlink(missing_ok=True)


def _gtts_single(text: str) -> bytes | None:
    """Генерирует один MP3 чанк через gTTS."""
    try:
        from gtts import gTTS
        buf = io.BytesIO()
        gTTS(text=text, lang=_LANG, slow=False).write_to_fp(buf)
        return buf.getvalue()
    except Exception as e:
        logger.warning("gTTS ошибка для чанка '%s...': %s", text[:30], e)
        return None


def _concat_mp3(files: list[Path]) -> bytes | None:
    """Склеивает MP3 файлы через ffmpeg concat demuxer."""
    if not _FFMPEG_BIN:
        return b"".join(f.read_bytes() for f in files)

    list_file = Path(tempfile.mktemp(suffix=".txt"))
    out_file  = Path(tempfile.mktemp(suffix="_merged.mp3"))
    try:
        list_file.write_text("\n".join(f"file '{p}'" for p in files))
        r = subprocess.run(
            [_FFMPEG_BIN, "-y", "-f", "concat", "-safe", "0",
             "-i", str(list_file), "-c", "copy", str(out_file)],
            capture_output=True, timeout=20,
        )
        if r.returncode == 0 and out_file.exists():
            return out_file.read_bytes()
        logger.warning("ffmpeg concat код %d: %s", r.returncode, r.stderr[-200:])
        return b"".join(f.read_bytes() for f in files)
    except Exception as e:
        logger.warning("ffmpeg concat ошибка: %s", e)
        return b"".join(f.read_bytes() for f in files)
    finally:
        list_file.unlink(missing_ok=True)
        out_file.unlink(missing_ok=True)


def _speed_up(mp3_bytes: bytes) -> bytes:
    """Ускоряет MP3 через ffmpeg atempo. x1.7 — один фильтр (макс 2.0)."""
    tmp_in = tmp_out = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
            f.write(mp3_bytes)
            tmp_in = Path(f.name)
        tmp_out = tmp_in.with_suffix(".fast.mp3")

        r = subprocess.run(
            [_FFMPEG_BIN, "-y", "-i", str(tmp_in),
             "-filter:a", f"atempo={_SPEED}", "-vn", str(tmp_out)],
            capture_output=True, timeout=20,
        )
        if r.returncode == 0 and tmp_out.exists():
            result = tmp_out.read_bytes()
            logger.debug("🔊 x%.1f: %d → %d байт", _SPEED, len(mp3_bytes), len(result))
            return result
        logger.warning("ffmpeg speed код %d", r.returncode)
        return mp3_bytes
    except Exception as e:
        logger.warning("ffmpeg speed ошибка: %s", e)
        return mp3_bytes
    finally:
        if tmp_in  and tmp_in.exists(): tmp_in.unlink(missing_ok=True)
        if tmp_out and tmp_out.exists(): tmp_out.unlink(missing_ok=True)


# ── Утилиты ───────────────────────────────────────────────────────────────────

def _split_sentences(text: str) -> list[str]:
    """
    Разбивает текст на чанки ≤ _CHUNK_MAX символов по границам предложений.
    Гарантирует что каждый чанк ≤ 450 байт URL (лимит gTTS для кириллицы).
    """
    raw = re.split(r'(?<=[.!?…])\s+', text)
    chunks: list[str] = []
    current = ""

    for sentence in raw:
        if not sentence.strip():
            continue
        # Длинное предложение — режем по запятым
        if len(sentence) > _CHUNK_MAX:
            parts = re.split(r'(?<=[,;])\s+', sentence)
            for part in parts:
                if len(current) + len(part) + 1 <= _CHUNK_MAX:
                    current = (current + " " + part).strip()
                else:
                    if current:
                        chunks.append(current)
                    # Если даже одна часть длиннее — режем жёстко
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


def _trim_to_words(text: str, max_words: int) -> str:
    """Обрезает по границе предложения не превышая max_words слов."""
    words = text.split()
    if len(words) <= max_words:
        return text
    trimmed = " ".join(words[:max_words])
    for sep in (".", "!", "?"):
        idx = trimmed.rfind(sep)
        if idx > len(trimmed) // 2:
            return trimmed[:idx + 1]
    return trimmed + "."


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
