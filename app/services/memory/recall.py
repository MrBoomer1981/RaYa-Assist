"""
Recall Memory — слой 2 из 3.

Эпизодическая память: сжатые разговоры (~8 ходов → 1 эпизод).
Поиск: BM25 (FTS5) → топ-20 → LLM rerank → топ-3.

Почему BM25 + LLM rerank, а не FAISS:
  - Railway: FAISS для 10k эпизодов = ~400MB RAM (free tier = 512MB total)
  - BM25 + LLM rerank качественнее чем FAISS без rerank
  - Ноль тяжёлых зависимостей
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
from pathlib import Path

from app.database import _conn, DB_PATH

logger = logging.getLogger(__name__)

_EPISODE_TURNS   = 8    # ходов разговора → 1 эпизод
_BM25_CANDIDATES = 20   # кандидатов из BM25
_RERANK_TOP      = 3    # финал после rerank
_MIN_SCORE       = 0.30  # порог rerank-score

# FTS5 спецсимволы которые нужно экранировать в MATCH-запросе
_FTS5_SPECIAL = re.compile(r'[()"\*\+\-\^~:,\[\]{}]')


def _init_tables() -> None:
    """
    Создаёт таблицы episodes + FTS5 индекс + триггеры.
    Использует отдельное соединение (не _conn()) — executescript()
    делает implicit COMMIT и несовместим с контекст-менеджером WAL.
    """
    con = sqlite3.connect(str(DB_PATH), timeout=15)
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.executescript("""
            CREATE TABLE IF NOT EXISTS episodes (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER NOT NULL,
                summary    TEXT    NOT NULL,
                key_facts  TEXT    DEFAULT '[]',
                topics     TEXT    DEFAULT '[]',
                turn_start INTEGER DEFAULT 0,
                turn_end   INTEGER DEFAULT 0,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                importance REAL     DEFAULT 3.0
            );

            CREATE INDEX IF NOT EXISTS idx_episodes_user
                ON episodes(user_id, created_at DESC);

            CREATE VIRTUAL TABLE IF NOT EXISTS episodes_fts
                USING fts5(
                    summary,
                    key_facts,
                    topics,
                    content='episodes',
                    content_rowid='id'
                );

            CREATE TRIGGER IF NOT EXISTS episodes_ai
                AFTER INSERT ON episodes BEGIN
                    INSERT INTO episodes_fts(rowid, summary, key_facts, topics)
                    VALUES (new.id, new.summary, new.key_facts, new.topics);
                END;

            CREATE TRIGGER IF NOT EXISTS episodes_au
                AFTER UPDATE ON episodes BEGIN
                    INSERT INTO episodes_fts(episodes_fts, rowid, summary, key_facts, topics)
                    VALUES ('delete', old.id, old.summary, old.key_facts, old.topics);
                    INSERT INTO episodes_fts(rowid, summary, key_facts, topics)
                    VALUES (new.id, new.summary, new.key_facts, new.topics);
                END;
        """)
    finally:
        con.close()


def save_episode(
    user_id: int,
    summary: str,
    key_facts: list[str],
    topics: list[str],
    turn_start: int = 0,
    turn_end: int = 0,
    importance: float = 3.0,
) -> int:
    """Сохраняет эпизод. Возвращает id."""
    with _conn() as con:
        cur = con.execute(
            """
            INSERT INTO episodes
                (user_id, summary, key_facts, topics, turn_start, turn_end, importance)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                summary,
                json.dumps(key_facts, ensure_ascii=False),
                json.dumps(topics, ensure_ascii=False),
                turn_start,
                turn_end,
                float(max(1.0, min(5.0, importance))),
            ),
        )
        eid = cur.lastrowid
    logger.info(
        "💾 Recall: эпизод #%d | user_id=%s | topics=%s", eid, user_id, topics
    )
    return eid


def bm25_search(user_id: int, query: str, limit: int = _BM25_CANDIDATES) -> list[dict]:
    """
    BM25 поиск через FTS5.
    BM25 score из SQLite — отрицательный (меньше = релевантнее).
    Нормализуем в 0..1 для единообразия с rerank_score.
    """
    safe_q = _build_fts5_query(query)
    if not safe_q:
        return []

    try:
        with _conn() as con:
            rows = con.execute(
                """
                SELECT e.id, e.summary, e.key_facts, e.topics,
                       e.created_at, e.importance,
                       bm25(episodes_fts) AS raw_bm25
                FROM episodes_fts
                JOIN episodes e ON episodes_fts.rowid = e.id
                WHERE episodes_fts MATCH ? AND e.user_id = ?
                ORDER BY raw_bm25          -- ASC: самые отрицательные = самые релевантные
                LIMIT ?
                """,
                (safe_q, user_id, limit),
            ).fetchall()
    except Exception as exc:
        logger.warning("recall: BM25 error q=%r : %s", safe_q, exc)
        return []

    if not rows:
        return []

    # Нормализация BM25 → 0..1
    raw_scores = [r[6] for r in rows]
    min_s, max_s = min(raw_scores), max(raw_scores)
    spread = max_s - min_s if max_s != min_s else 1.0

    return [
        {
            "id":          r[0],
            "summary":     r[1],
            "key_facts":   json.loads(r[2] or "[]"),
            "topics":      json.loads(r[3] or "[]"),
            "created_at":  r[4],
            "importance":  float(r[5] or 3.0),
            # инвертируем: самые отрицательные → ближе к 1.0
            "bm25_score":  1.0 - (r[6] - min_s) / spread,
        }
        for r in rows
    ]


async def search(
    user_id: int,
    query: str,
    llm,
    top_k: int = _RERANK_TOP,
) -> list[dict]:
    """
    Гибридный поиск: BM25 → LLM rerank → топ-K.
    Если кандидатов ≤ top_k — rerank не нужен, возвращаем сразу.
    """
    candidates = bm25_search(user_id, query)
    if not candidates:
        return []
    if len(candidates) <= top_k:
        return candidates

    try:
        return await _llm_rerank(query, candidates, llm, top_k)
    except Exception as exc:
        logger.warning("recall: rerank failed (%s), fallback BM25 order", exc)
        return candidates[:top_k]


async def _llm_rerank(
    query: str,
    candidates: list[dict],
    llm,
    top_k: int,
) -> list[dict]:
    """
    LLM оценивает релевантность кандидатов.
    Использует fast_llm (8b) — не тратим токены большой модели.
    """
    from langchain_core.messages import HumanMessage
    from app.utils import strip_json

    snippets = []
    for i, c in enumerate(candidates):
        facts_str = "; ".join(c["key_facts"][:3]) if c["key_facts"] else ""
        tail = f"  ({facts_str})" if facts_str else ""
        snippets.append(f"[{i}] {c['summary'][:200]}{tail}")

    prompt = (
        f'Запрос: "{query[:200]}"\n\n'
        f"Оцени релевантность каждого эпизода памяти (0.0–1.0):\n"
        + "\n".join(snippets)
        + '\n\nВерни ТОЛЬКО JSON: {"scores": [0.8, 0.1, ...]}'
        " — список в том же порядке. Без пояснений."
    )

    response = await llm.ainvoke([HumanMessage(content=prompt)])
    raw    = strip_json(str(response.content))
    data   = json.loads(raw)
    scores = data.get("scores", [])

    if len(scores) != len(candidates):
        logger.warning(
            "recall: rerank returned %d scores for %d candidates — fallback",
            len(scores), len(candidates),
        )
        return candidates[:top_k]

    scored = [
        {**c, "rerank_score": max(0.0, min(1.0, float(s)))}
        for c, s in zip(candidates, scores)
    ]
    scored.sort(key=lambda x: x["rerank_score"], reverse=True)
    return [s for s in scored[:top_k] if s["rerank_score"] >= _MIN_SCORE]


def get_recent(user_id: int, limit: int = 5) -> list[dict]:
    """Последние N эпизодов — для /memory команды."""
    with _conn() as con:
        rows = con.execute(
            """
            SELECT id, summary, topics, created_at, importance
            FROM episodes
            WHERE user_id = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (user_id, limit),
        ).fetchall()
    return [
        {
            "id":         r[0],
            "summary":    r[1],
            "topics":     json.loads(r[2] or "[]"),
            "created_at": r[3],
            "importance": float(r[4] or 3.0),
        }
        for r in rows
    ]


def count(user_id: int) -> int:
    with _conn() as con:
        return con.execute(
            "SELECT COUNT(*) FROM episodes WHERE user_id = ?", (user_id,)
        ).fetchone()[0]


def _build_fts5_query(text: str) -> str:
    """
    Строит безопасный FTS5 MATCH запрос.
    - Убирает спецсимволы FTS5
    - Оборачивает слова длиннее 2 символов в кавычки (phrase search)
    - Возвращает пустую строку если нечего искать
    """
    cleaned = _FTS5_SPECIAL.sub(" ", text)
    words   = [w for w in cleaned.split() if w]
    tokens  = [f'"{w}"' if len(w) > 2 else w for w in words[:12]]
    return " ".join(tokens).strip()
