"""
utils.py — общие утилиты используемые несколькими модулями.
Вынесено сюда чтобы избежать дублирования кода.
"""
import json
import logging
import re
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

_REMINDER_RE = re.compile(r"<reminder>(.*?)</reminder>", re.DOTALL)
_REMINDER_CLEAN_RE = re.compile(r"\s*<reminder>.*?</reminder>", re.DOTALL)
_TIME_FMT = "%Y-%m-%d %H:%M:%S"


_VALID_RECURRENCES = {"daily", "weekly", "weekday", "monthly"}


def parse_reminder(raw: str, now_utc: datetime) -> dict | None:
    """
    Извлекает JSON из <reminder>...</reminder>.
    Возвращает {"text", "remind_at", "recurrence"} или None.
    """
    match = _REMINDER_RE.search(raw)
    if not match:
        return None
    try:
        data = json.loads(match.group(1).strip())
        if not isinstance(data, dict):
            return None
        if "text" not in data or "remind_at" not in data:
            return None

        remind_str = str(data["remind_at"]).strip()
        if len(remind_str) == 16:
            remind_str += ":00"
        data["remind_at"] = remind_str

        remind_dt = datetime.strptime(remind_str, _TIME_FMT)
        if remind_dt <= now_utc:
            logger.warning("parse_reminder: время в прошлом %s", remind_str)
            return None

        recurrence = data.get("recurrence")
        if recurrence and recurrence not in _VALID_RECURRENCES:
            logger.warning("parse_reminder: неизвестный recurrence=%s", recurrence)
            recurrence = None
        data["recurrence"] = recurrence or None

        logger.info("⏰ '%s' на %s UTC recurrence=%s",
                    data["text"], remind_str, data["recurrence"])
        return data
    except (json.JSONDecodeError, ValueError) as e:
        logger.warning("parse_reminder: ошибка парсинга: %s", e)
        return None


def clean_reminder_tag(text: str) -> str:
    """Убирает тег <reminder>...</reminder> из текста ответа."""
    return _REMINDER_CLEAN_RE.sub("", text).strip()


def build_reminder_prompt_block(now_utc: datetime) -> str:
    """
    Формирует блок инструкций по напоминаниям для системного промпта.
    Передаём явное UTC время — модель не угадывает его.
    """
    ex_5min     = (now_utc + timedelta(minutes=5)).strftime(_TIME_FMT)
    ex_1h       = (now_utc + timedelta(hours=1)).strftime(_TIME_FMT)
    ex_tomorrow = (now_utc + timedelta(days=1)).strftime("%Y-%m-%d") + " 09:00:00"

    return f"""

--- НАПОМИНАНИЯ ---
Текущее время UTC: {now_utc.strftime(_TIME_FMT)}

Если пользователь просит напомнить — включи тег:
<reminder>{{"text": "текст", "remind_at": "YYYY-MM-DD HH:MM:SS", "recurrence": null}}</reminder>

Для ПОВТОРЯЮЩИХСЯ напоминаний используй поле recurrence:
  "daily"   — каждый день
  "weekly"  — каждую неделю
  "weekday" — по будням (пн-пт)
  "monthly" — раз в месяц
  null      — одноразовое (по умолчанию)

Примеры:
- "через 5 минут"           → remind_at={ex_5min}, recurrence=null
- "через час"               → remind_at={ex_1h}, recurrence=null
- "завтра в 9"              → remind_at={ex_tomorrow}, recurrence=null
- "каждый день в 8 утра"    → remind_at=ближайшее 05:00 UTC, recurrence="daily"
- "по будням в 9"           → remind_at=ближайший будний 06:00 UTC, recurrence="weekday"
- "каждый понедельник в 10" → remind_at=следующий пн 07:00 UTC, recurrence="weekly"

Если напоминания нет — НЕ включай тег.
--- КОНЕЦ ---"""


# ── Чистка ответа перед отправкой пользователю ────────────────────────────────

# URL: http(s)://... или www....
_URL_RE = re.compile(
    r"https?://\S+|www\.\S+",
    re.IGNORECASE,
)

# Markdown ссылки: [текст](url) → оставляем только текст
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\(https?://[^\)]+\)")

# Строки-источники — расширенный список
_SOURCE_LINE_RE = re.compile(
    r"^[ \t]*(источник|источники|source|sources|url|urls|ссылка|ссылки|"
    r"подробнее|читать далее|read more|узнать больше|more info|"
    r"по данным|according to|via|from)[:\s].+$",
    re.IGNORECASE | re.MULTILINE,
)

# Inline-ссылки на источники внутри предложений:
# "согласно данным с X," / "по данным X," / "на сайте X можно"
_INLINE_SOURCE_RE = re.compile(
    r",?\s*(согласно данным с|согласно данным|по данным|"
    r"на сайте \S+ можно [^,\.]+|"
    r"также,?\s*согласно|кроме того,?\s*на сайте \S+)[^,\.]*[,\.]?",
    re.IGNORECASE,
)

# "Также, согласно..." в начале предложения — убираем всё предложение
_ALSO_SOURCE_SENT_RE = re.compile(
    r"[А-ЯЁ][^.!?]*(?:согласно данным|по данным|на сайте \S+ можно найти)[^.!?]*[.!?]",
    re.IGNORECASE,
)

# Сноски и нумерованные источники: [1] ..., ¹ ..., * источник
_FOOTNOTE_RE = re.compile(
    r"^[ \t]*(\[\d+\]|\d+\.|[\*†‡§]|¹|²|³)[ \t]+https?://\S+.*$",
    re.IGNORECASE | re.MULTILINE,
)

# Блоки «Источники:» с несколькими строками
_SOURCE_BLOCK_RE = re.compile(
    r"(источники?|sources?|ссылки?):?\s*\n([ \t]*[\-\*\d\.\[].+\n?)+",
    re.IGNORECASE,
)

# Служебные теги модели — не для пользователя
_SERVICE_TAGS_RE = re.compile(
    r"<(emotion|reminder|task|done|delete|save_tasks)>.*?</(emotion|reminder|task|done|delete|save_tasks)>",
    re.DOTALL | re.IGNORECASE,
)

# Три и более пустых строки → две
_EXCESS_NEWLINES_RE = re.compile(r"\n{3,}")


def clean_reply(text: str) -> str:
    """
    Чистит ответ модели перед отправкой пользователю:
    - markdown-ссылки [текст](url) → просто текст
    - строки «Источник: ...», «URL: ...»
    - голые URL (http/https/www)
    - служебные теги <emotion>, <reminder>, <task> и т.д.
    - лишние пустые строки (3+ → 2)
    """
    # Markdown ссылки → только анкорный текст
    text = _MD_LINK_RE.sub(r"\1", text)

    # Предложения целиком с источниками ("согласно данным с X")
    text = _ALSO_SOURCE_SENT_RE.sub("", text)
    # Блоки источников целиком
    text = _SOURCE_BLOCK_RE.sub("", text)
    # Отдельные строки-источники
    text = _SOURCE_LINE_RE.sub("", text)
    # Inline-ссылки на источники внутри предложений
    text = _INLINE_SOURCE_RE.sub("", text)
    # Нумерованные сноски с URL
    text = _FOOTNOTE_RE.sub("", text)

    # Голые URL
    text = _URL_RE.sub("", text)

    # Служебные теги
    text = _SERVICE_TAGS_RE.sub("", text)

    # Артефакты: пустые скобки (), []
    text = re.sub(r"\(\s*\)", "", text)
    text = re.sub(r"\[\s*\]", "", text)

    # Лишние пустые строки
    text = _EXCESS_NEWLINES_RE.sub("\n\n", text)

    return text.strip()

# ── Общие утилиты ────────────────────────────────────────────────────────────

# Словарь повторений — используется в handlers и proactive
RECUR_RU: dict[str, str] = {
    "daily":   "каждый день",
    "weekly":  "каждую неделю",
    "weekday": "по будням",
    "monthly": "каждый месяц",
}


def strip_json(raw: str) -> str:
    """Убирает ```json ... ``` обёртку из ответа LLM."""
    return raw.strip().replace("```json", "").replace("```", "").strip()
