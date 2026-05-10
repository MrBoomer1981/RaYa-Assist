"""
SQLite-backed knowledge base with FTS5 full-text search.
No external dependencies — search is handled natively by SQLite.
"""
import json
import sqlite3
import time
from dataclasses import dataclass
from typing import List, Optional, Tuple

from deeper.utils.logger import get_logger

logger = get_logger("knowledge_base")


@dataclass
class Research:
    id: int
    title: str
    summary: str
    report: str
    sources: List[str]
    timestamp: float

    def formatted_timestamp(self) -> str:
        import datetime
        return datetime.datetime.fromtimestamp(self.timestamp).strftime("%Y-%m-%d %H:%M")


class KnowledgeBase:
    def __init__(self, db_path: str, embedding_service=None, max_researches: int = 20) -> None:
        self.db_path = db_path
        self.max_researches = max_researches
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            # Main table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS researches (
                    id        INTEGER PRIMARY KEY AUTOINCREMENT,
                    title     TEXT NOT NULL,
                    summary   TEXT NOT NULL,
                    report    TEXT NOT NULL,
                    sources   TEXT NOT NULL,
                    timestamp REAL NOT NULL
                )
            """)
            # FTS5 virtual table for full-text search
            conn.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS researches_fts
                USING fts5(
                    title,
                    summary,
                    content='researches',
                    content_rowid='id'
                )
            """)
            # Keep FTS index in sync via triggers
            conn.execute("""
                CREATE TRIGGER IF NOT EXISTS researches_ai
                AFTER INSERT ON researches BEGIN
                    INSERT INTO researches_fts(rowid, title, summary)
                    VALUES (new.id, new.title, new.summary);
                END
            """)
            conn.execute("""
                CREATE TRIGGER IF NOT EXISTS researches_ad
                AFTER DELETE ON researches BEGIN
                    INSERT INTO researches_fts(researches_fts, rowid, title, summary)
                    VALUES ('delete', old.id, old.title, old.summary);
                END
            """)
            conn.commit()
        logger.info("Knowledge base initialized at {}", self.db_path)

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    async def save_research(
        self,
        title: str,
        summary: str,
        report: str,
        sources: List[str],
    ) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO researches (title, summary, report, sources, timestamp)
                VALUES (?, ?, ?, ?, ?)
                """,
                (title, summary, report, json.dumps(sources), time.time()),
            )
            conn.commit()
            research_id = cur.lastrowid

        logger.info("Saved research #{}: {}", research_id, title[:60])
        await self._enforce_limit()
        return research_id

    async def _enforce_limit(self) -> None:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id FROM researches ORDER BY timestamp ASC"
            ).fetchall()
        if len(rows) > self.max_researches:
            for row in rows[:len(rows) - self.max_researches]:
                await self.delete_research(row["id"])
                logger.info("Auto-deleted oldest research #{}", row["id"])

    def get_research(self, research_id: int) -> Optional[Research]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM researches WHERE id = ?", (research_id,)
            ).fetchone()
        return self._row_to_research(row) if row else None

    def list_researches(self) -> List[Research]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM researches ORDER BY timestamp DESC"
            ).fetchall()
        return [self._row_to_research(r) for r in rows]

    async def delete_research(self, research_id: int) -> bool:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM researches WHERE id = ?", (research_id,))
            conn.commit()
        if cur.rowcount == 0:
            return False
        logger.info("Deleted research #{}", research_id)
        return True

    # ------------------------------------------------------------------
    # FTS5 Search
    # ------------------------------------------------------------------

    async def semantic_search(
        self, query: str, top_k: int = 5
    ) -> List[Tuple[Research, float]]:
        """
        Full-text search using SQLite FTS5.
        Returns list of (Research, score) sorted by relevance.
        """
        with self._connect() as conn:
            # FTS5 MATCH with BM25 ranking (lower = more relevant)
            rows = conn.execute(
                """
                SELECT r.*, bm25(researches_fts) AS score
                FROM researches_fts
                JOIN researches r ON researches_fts.rowid = r.id
                WHERE researches_fts MATCH ?
                ORDER BY score
                LIMIT ?
                """,
                (self._fts_query(query), top_k),
            ).fetchall()

        results = []
        for row in rows:
            research = self._row_to_research(row)
            score = abs(float(row["score"]))  # BM25 is negative in SQLite
            results.append((research, score))

        if not results:
            # Fallback: return most recent researches if no FTS match
            recent = self.list_researches()[:top_k]
            results = [(r, 999.0) for r in recent]

        logger.info("FTS search '{}' → {} results", query[:40], len(results))
        return results

    @staticmethod
    def _fts_query(query: str) -> str:
        """Sanitize query for FTS5 MATCH syntax."""
        # Remove FTS5 special chars, wrap each word
        words = [w for w in query.split() if w.isalnum() or len(w) > 2]
        if not words:
            return query
        return " OR ".join(words)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_research(row: sqlite3.Row) -> Research:
        return Research(
            id=row["id"],
            title=row["title"],
            summary=row["summary"],
            report=row["report"],
            sources=json.loads(row["sources"]),
            timestamp=row["timestamp"],
        )
