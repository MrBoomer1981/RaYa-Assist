"""
main.py — точка входа. Только запуск.

Вся логика в app/core.py.
"""
import asyncio
import logging
import warnings

warnings.filterwarnings("ignore", message=".*Pydantic V1.*")

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logging.getLogger("aiogram").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)

from app.core import Core


if __name__ == "__main__":
    asyncio.run(Core().start())
