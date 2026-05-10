"""
Core Memory — слой 1 из 3.

Всегда присутствует в системном промпте. ~400 токенов.
Два блока как в Hermes/MemGPT:
  [HUMAN]   — ключевые факты о пользователе (до 15 записей, importance 1–5)

Принципы:
  - importance 1–5 определяет приоритет при вытеснении
  - Decay: запускается не чаще раза в 6ч per user (не на каждом чтении)
  - При переполнении → наименее важные уходят в Recall
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from app.database import _conn, upsert_memory, delete_memory_entry

logger = logging.getLogger(__name__)

_CORE_MAX_FACTS        = 15
_IMPORTANCE_DECAY_DAYS = 30
_DECAY_AMOUNT          = 0.5
_DECAY_THROTTLE_H      = 6   # decay не чаще раза в N часов per user

# user_id → datetime последнего decay
_last_decay: dict[int, datetime] = {}


def _ensure_importance_column() -> None:
    """Добавляет importance и last_accessed в structured_memory если нет."""
    with _conn() as con:
        cols = {r[1] for r in con.execute("PRAGMA table_info(structured_memory)").fetchall()}
        if "importance" not in cols:
            con.execute(
                "ALTER TABLE structured_memory ADD COLUMN importance REAL DEFAULT 3.0"
            )
            logger.info("✅ Migration: structured_memory.importance добавлен")
        if "last_accessed" not in cols:
            con.execute(
                "ALTER TABLE structured_memory "
                "ADD COLUMN last_accessed DATETIME DEFAULT CURRENT_TIMESTAMP"
            )
            logger.info("✅ Migration: structured_memory.last_accessed добавлен")


def get_core_facts(user_id: int, limit: int = _CORE_MAX_FACTS) -> list[dict]:
    """
    Топ-N фактов по важности.
    Decay запускается не чаще раза в _DECAY_THROTTLE_H часов.
    """
    _maybe_decay(user_id)
    with _conn() as con:
        rows = con.execute(
            """
            SELECT category, key, value,
                   COALESCE(importance, 3.0)    AS importance,
                   last_accessed
            FROM structured_memory
            WHERE user_id = ?
            ORDER BY importance DESC, updated_at DESC
            LIMIT ?
            """,
            (user_id, limit),
        ).fetchall()
    return [
        {
            "category":     r[0],
            "key":          r[1],
            "value":        r[2],
            "importance":   float(r[3]),
            "last_accessed": r[4],
        }
        for r in rows
    ]


def upsert_core_fact(
    user_id: int,
    category: str,
    key: str,
    value: str,
    importance: float = 3.0,
) -> None:
    """
    Сохраняет/обновляет факт одним запросом.
    Использует INSERT OR REPLACE + сразу ставит importance.
    """
    importance = float(max(1.0, min(5.0, importance)))
    with _conn() as con:
        con.execute(
            """
            INSERT INTO structured_memory
                (user_id, category, key, value, importance, last_accessed, updated_at)
            VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            ON CONFLICT(user_id, category, key)
            DO UPDATE SET
                value        = excluded.value,
                importance   = excluded.importance,
                last_accessed = CURRENT_TIMESTAMP,
                updated_at   = CURRENT_TIMESTAMP
            """,
            (user_id, category, key, value, importance),
        )


def boost_importance(
    user_id: int, category: str, key: str, amount: float = 1.0
) -> None:
    """Повышает важность факта при повторном упоминании."""
    with _conn() as con:
        con.execute(
            """
            UPDATE structured_memory
            SET importance    = MIN(5.0, COALESCE(importance, 3.0) + ?),
                last_accessed = CURRENT_TIMESTAMP
            WHERE user_id = ? AND category = ? AND key = ?
            """,
            (amount, user_id, category, key),
        )


def evict_to_recall(user_id: int) -> list[dict]:
    """
    Вытесняет наименее важные факты если Core > _CORE_MAX_FACTS.
    Одна транзакция — нет race condition.
    """
    with _conn() as con:
        # Всё в одной транзакции
        count = con.execute(
            "SELECT COUNT(*) FROM structured_memory WHERE user_id = ?",
            (user_id,),
        ).fetchone()[0]

        if count <= _CORE_MAX_FACTS:
            return []

        to_evict_n = count - _CORE_MAX_FACTS
        rows = con.execute(
            """
            SELECT category, key, value, COALESCE(importance, 3.0)
            FROM structured_memory
            WHERE user_id = ?
            ORDER BY importance ASC, updated_at ASC
            LIMIT ?
            """,
            (user_id, to_evict_n),
        ).fetchall()

        evicted = [
            {"category": r[0], "key": r[1], "value": r[2], "importance": float(r[3])}
            for r in rows
        ]
        if evicted:
            con.executemany(
                "DELETE FROM structured_memory WHERE user_id = ? AND category = ? AND key = ?",
                [(user_id, e["category"], e["key"]) for e in evicted],
            )

    if evicted:
        logger.info("♻️ Core→Recall: вытеснено %d фактов | user_id=%s", len(evicted), user_id)
    return evicted


def format_for_prompt(user_id: int) -> str:
    """Форматирует Core Memory для системного промпта (~400 токенов)."""
    facts = get_core_facts(user_id)
    if not facts:
        return ""

    _LABELS = {
        "facts":       "О тебе",
        "interests":   "Интересы",
        "projects":    "Проекты",
        "skills":      "Навыки",
        "preferences": "Предпочтения",
        "goals":       "Цели",
        "context":     "Сейчас",
        "decisions":   "Решения",
    }

    by_cat: dict[str, list[str]] = {}
    for f in facts:
        label = _LABELS.get(f["category"], f["category"])
        by_cat.setdefault(label, []).append(f["value"])

    lines = ["<core_memory>"]
    for label, vals in by_cat.items():
        lines.append(f"  {label}: {'; '.join(vals)}")
    lines.append("</core_memory>")
    return "\n".join(lines)


# ── Внутренние ────────────────────────────────────────────────────────────────

def _maybe_decay(user_id: int) -> None:
    """Запускает decay не чаще раза в _DECAY_THROTTLE_H часов per user."""
    now = datetime.utcnow()
    last = _last_decay.get(user_id)
    if last and (now - last).total_seconds() < _DECAY_THROTTLE_H * 3600:
        return
    _last_decay[user_id] = now
    _apply_decay(user_id)


def _apply_decay(user_id: int) -> None:
    """SQL UPDATE — снижает importance старых фактов."""
    cutoff = (
        datetime.utcnow() - timedelta(days=_IMPORTANCE_DECAY_DAYS)
    ).strftime("%Y-%m-%d %H:%M:%S")
    with _conn() as con:
        con.execute(
            """
            UPDATE structured_memory
            SET importance = MAX(1.0, COALESCE(importance, 3.0) - ?)
            WHERE user_id = ?
              AND last_accessed < ?
              AND COALESCE(importance, 3.0) > 1.0
            """,
            (_DECAY_AMOUNT, user_id, cutoff),
        )
