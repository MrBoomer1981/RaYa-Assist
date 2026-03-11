"""
router_calibration.py — обучение роутера на ошибках.

Принцип:
  - После каждого ответа агента — смотрим на следующее сообщение Сократа
  - Если он явно переспрашивает или недоволен — фиксируем неверный маршрут
  - Накапливаем паттерны в БД → роутер учитывает их при следующем запросе

Что считается признаком неверного роутинга:
  - "это не то", "не об этом", "ты не понял", "другой вопрос"
  - Сократ повторяет вопрос другими словами
  - Короткий вопрос после длинного ответа не по теме

Хранение: таблица router_feedback в SQLite
  message_pattern → правильный агент (с весом)
"""
import logging
import re
import sqlite3
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# Сигналы неверного роутинга
_MISMATCH_RE = re.compile(
    r"\b(не об этом|не то|не понял|другой вопрос|я спрашивал|"
    r"имел в виду|переформулирую|не так понял|снова спрошу|"
    r"другое имел|нет не то|совсем не то)\b",
    re.IGNORECASE,
)


def _db_path() -> Path:
    from app.database import DB_PATH
    return DB_PATH


def init_calibration_table() -> None:
    """Создаёт таблицу если не существует."""
    try:
        with sqlite3.connect(str(_db_path())) as con:
            con.execute("""
                CREATE TABLE IF NOT EXISTS router_feedback (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id      INTEGER NOT NULL,
                    message_hash TEXT    NOT NULL,
                    keywords     TEXT    NOT NULL,
                    wrong_agent  TEXT    NOT NULL,
                    right_agent  TEXT,
                    created_at   DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            con.execute("""
                CREATE INDEX IF NOT EXISTS idx_rf_keywords
                ON router_feedback(keywords)
            """)
    except Exception:
        logger.exception("calibration: ошибка создания таблицы")


class RouterCalibration:
    """Отслеживает ошибки роутера и предоставляет подсказки."""

    def __init__(self) -> None:
        init_calibration_table()
        # Кэш последнего маршрута на сессию: user_id → (message, agent)
        self._last_route: dict[int, tuple[str, str]] = {}

    def record_route(self, user_id: int, message: str, agent: str) -> None:
        """Запоминаем что было отправлено какому агенту."""
        self._last_route[user_id] = (message, agent)

    def check_mismatch(self, user_id: int, next_message: str) -> bool:
        """
        Проверяем: следующее сообщение Сократа — сигнал недовольства?
        Если да — сохраняем фидбэк в БД.
        """
        if user_id not in self._last_route:
            return False

        is_mismatch = bool(_MISMATCH_RE.search(next_message))
        if not is_mismatch:
            return False

        prev_msg, wrong_agent = self._last_route[user_id]
        self._save_feedback(user_id, prev_msg, wrong_agent)
        logger.info(
            "📊 Router calibration: зафиксирован неверный роутинг '%s' → '%s'",
            prev_msg[:40], wrong_agent,
        )
        return True

    def get_hint(self, message: str) -> str | None:
        """
        Возвращает подсказку для роутера на основе накопленных ошибок.
        Формат: 'сообщения похожие на X обычно не для агента Y'
        """
        try:
            keywords = self._extract_keywords(message)
            if not keywords:
                return None

            with sqlite3.connect(str(_db_path())) as con:
                rows = con.execute("""
                    SELECT wrong_agent, COUNT(*) as cnt
                    FROM router_feedback
                    WHERE keywords LIKE ?
                    GROUP BY wrong_agent
                    ORDER BY cnt DESC
                    LIMIT 3
                """, (f"%{keywords[0]}%",)).fetchall()

            if not rows:
                return None

            hints = [
                f"избегай агента '{row[0]}' для сообщений о '{keywords[0]}' "
                f"(зафиксировано {row[1]} ошибок)"
                for row in rows if row[1] >= 2
            ]
            return "\n".join(hints) if hints else None

        except Exception:
            return None

    def _save_feedback(self, user_id: int, message: str, wrong_agent: str) -> None:
        try:
            keywords = " ".join(self._extract_keywords(message))
            msg_hash = str(hash(message.strip().lower()))[:12]

            with sqlite3.connect(str(_db_path())) as con:
                con.execute("""
                    INSERT INTO router_feedback
                        (user_id, message_hash, keywords, wrong_agent)
                    VALUES (?, ?, ?, ?)
                """, (user_id, msg_hash, keywords, wrong_agent))
        except Exception:
            logger.exception("calibration: ошибка сохранения фидбэка")

    @staticmethod
    def _extract_keywords(message: str) -> list[str]:
        """Извлекает ключевые слова из сообщения (простая версия)."""
        stop = {
            "и", "в", "на", "с", "по", "для", "что", "как", "это",
            "не", "но", "а", "я", "ты", "мне", "мой", "моя",
        }
        words = re.findall(r"\b[а-яёa-z]{4,}\b", message.lower())
        return [w for w in words if w not in stop][:5]
