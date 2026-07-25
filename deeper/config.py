"""
deeper/config.py — конфигурация DEEper модуля.

Читает из того же .env что и Рая.
Пути к данным — в data/deeper/ (персистентный том на Railway).

Дорабатывать можно свободно — Рая импортирует только DeeperConfig.
"""
import os
from dataclasses import dataclass
from typing import Dict
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# Базовая директория данных — data/deeper/ внутри проекта
# На Railway монтируется как persistent volume на /app/data
_BASE_DATA = Path(os.getenv("DEEPER_DATA_DIR", "data/deeper"))


@dataclass
class ResearchMode:
    name: str
    label: str
    description: str
    search_queries: int
    max_pages: int
    max_chunks_per_page: int
    timeout_sec: int = 720  # общий потолок на bridge.research() для этого режима


RESEARCH_MODES: Dict[str, ResearchMode] = {
    "simple": ResearchMode(
        name="simple",
        label="🟢 Простой",
        description="Быстрый обзор темы — только самое главное",
        search_queries=5,
        max_pages=10,
        max_chunks_per_page=3,
        timeout_sec=360,   # 6 минут — вдвое больше старой оценки в ~3
    ),
    "deep": ResearchMode(
        name="deep",
        label="🔵 Углублённый",
        description="Баланс глубины и скорости — несколько источников с разных сторон",
        search_queries=15,
        max_pages=30,
        max_chunks_per_page=5,
        timeout_sec=720,   # 12 минут
    ),
    "study": ResearchMode(
        name="study",
        label="🟣 Изучение",
        description="Максимально подробно — для серьёзного погружения в тему",
        search_queries=20,
        max_pages=50,
        max_chunks_per_page=7,
        timeout_sec=1200,  # 20 минут — больше всего чанков → больше риск упереться в рейт-лимит
    ),
}


@dataclass
class DeeperConfig:
    groq_api_key:   str
    tavily_api_key: str

    primary_model: str = "llama-3.3-70b-versatile"
    fast_model:    str = "llama-3.1-8b-instant"

    pages_per_query:  int = 2
    max_researches:   int = 50        # лимит хранимых исследований
    chunk_size:       int = 800
    chunk_overlap:    int = 150
    max_pdf_size_mb:  int = 20

    scrape_timeout: int = 15
    scrape_retries: int = 3

    # Пути к данным (Railway persistent volume)
    db_path:         str = str(_BASE_DATA / "db.sqlite")
    faiss_index_path: str = str(_BASE_DATA / "faiss.index")
    logs_dir:        str = "logs"

    def ensure_dirs(self) -> None:
        """Создаёт директории если не существуют."""
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_env(cls) -> "DeeperConfig":
        groq_key   = os.getenv("GROQ_API_KEY", "")
        tavily_key = os.getenv("TAVILY_API_KEY", "")
        if not groq_key:
            raise ValueError("GROQ_API_KEY не задан")
        return cls(
            groq_api_key=groq_key,
            tavily_api_key=tavily_key,
            primary_model=os.getenv("MODEL_NAME", "llama-3.3-70b-versatile"),
            fast_model=os.getenv("ROUTER_MODEL", "llama-3.1-8b-instant"),
            max_researches=int(os.getenv("DEEPER_MAX_RESEARCHES", "50")),
            max_pdf_size_mb=int(os.getenv("DEEPER_MAX_PDF_MB", "20")),
            db_path=str(_BASE_DATA / "db.sqlite"),
            faiss_index_path=str(_BASE_DATA / "faiss.index"),
        )


# Синглтон — создаётся один раз при импорте
deeper_config = DeeperConfig.from_env()
