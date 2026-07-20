"""
URL-based page cache backed by SQLite.
Avoids re-scraping the same URLs across research sessions.
"""
import hashlib
import sqlite3
import time
from contextlib import contextmanager
from typing import Optional

from deeper.utils.logger import get_logger

logger = get_logger("cache_manager")


class CacheManager:
    """Manages a persistent cache of scraped web pages in SQLite."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._init_db()

    @contextmanager
    def _connect(self):
        """
        Раньше возвращала голый sqlite3.Connection, используемый как
        `with self._connect() as conn:` — контекстный менеджер
        sqlite3.Connection управляет только commit/rollback, но НЕ
        закрывает соединение (та же копипаста, что в knowledge_base.py
        и memory.py). При активном deep research (кэш проверяется на
        КАЖДЫЙ URL перед скрейпингом) утекало бы особенно быстро. Теперь
        настоящий контекстный менеджер: коммитит/откатывает и всегда закрывает.
        """
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS page_cache (
                    url       TEXT PRIMARY KEY,
                    url_hash  TEXT NOT NULL,
                    content   TEXT NOT NULL,
                    timestamp REAL NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_cache_hash ON page_cache(url_hash)"
            )
            conn.commit()
        logger.debug("Page cache DB initialized at {}", self.db_path)

    @staticmethod
    def _hash_url(url: str) -> str:
        return hashlib.sha256(url.encode()).hexdigest()

    def get(self, url: str) -> Optional[str]:
        """Return cached content for a URL, or None if not cached."""
        url_hash = self._hash_url(url)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT content FROM page_cache WHERE url_hash = ?", (url_hash,)
            ).fetchone()
        if row:
            logger.debug("Cache HIT: {}", url[:80])
            return row["content"]
        logger.debug("Cache MISS: {}", url[:80])
        return None

    def set(self, url: str, content: str) -> None:
        """Store page content in cache."""
        url_hash = self._hash_url(url)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO page_cache (url, url_hash, content, timestamp)
                VALUES (?, ?, ?, ?)
                """,
                (url, url_hash, content, time.time()),
            )
            conn.commit()
        logger.debug("Cached URL: {}", url[:80])

    def clear_old(self, max_age_days: int = 7) -> int:
        """Remove cache entries older than max_age_days. Returns deleted count."""
        cutoff = time.time() - max_age_days * 86400
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM page_cache WHERE timestamp < ?", (cutoff,)
            )
            conn.commit()
        logger.info("Cleared {} stale cache entries", cur.rowcount)
        return cur.rowcount

    def size(self) -> int:
        """Return total number of cached URLs."""
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) as n FROM page_cache").fetchone()
        return row["n"]
