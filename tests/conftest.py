"""
conftest.py — общие фикстуры для тестов.

Ключевой момент: несколько модулей (`recall.py`, `personality_service.py`,
`llm_pipeline.py`, `proactive_service.py`) делают `from app.database import DB_PATH`
и получают СВОЮ копию значения на момент импорта, а не читают
`app.database.DB_PATH` напрямую. Поэтому фикстура `temp_db` патчит DB_PATH
во всех этих модулях, а не только в `app.database`.
"""
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Переменные окружения должны быть выставлены ДО первого импорта app.*
os.environ.setdefault("TELEGRAM_TOKEN", "123456:TEST")
os.environ.setdefault("GROQ_API_KEY", "test-groq-key")

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Модули, которые копируют DB_PATH себе при импорте (см. докстринг выше)
_DB_PATH_ALIAS_MODULES = [
    "app.services.memory.recall",
    "app.personality_service",
    "app.llm_pipeline",
    "app.proactive_service",
]


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """
    Изолированная SQLite БД на тест — со всеми таблицами и миграциями,
    как при настоящем старте бота через init_db().
    """
    import importlib
    import app.database as database

    db_file = tmp_path / "test.db"
    monkeypatch.setattr(database, "DB_PATH", db_file)

    for mod_name in _DB_PATH_ALIAS_MODULES:
        mod = importlib.import_module(mod_name)
        if hasattr(mod, "DB_PATH"):
            monkeypatch.setattr(mod, "DB_PATH", db_file)

    database.init_db()

    # LRU-кэш имени пользователя общий на процесс — обязательно сбрасываем
    database._cached_user_name.cache_clear()

    yield database

    database._cached_user_name.cache_clear()


@pytest.fixture
def temp_settings(tmp_path, monkeypatch):
    """Изолированный JSON-файл пользовательских настроек на тест."""
    import app.settings as settings_module

    settings_file = tmp_path / "user_settings.json"
    monkeypatch.setattr(settings_module, "_SETTINGS_FILE", settings_file)
    monkeypatch.setattr(settings_module, "_settings", None)

    yield settings_module

    monkeypatch.setattr(settings_module, "_settings", None)


def make_llm_response(content: str):
    """Фейковый ответ LLM — как объект с .content, аналог AIMessage/ChatResult."""
    return MagicMock(content=content)


@pytest.fixture
def llm_response():
    """Фабрика фейковых LLM-ответов: llm_response("текст") -> MagicMock(content=...)."""
    return make_llm_response


@pytest.fixture(scope="session")
def registered_dispatcher():
    """
    Строит Dispatcher через Core()._build_dispatcher() — тот же путь, что и в
    проде (не напрямую через handlers.register()), значит middleware тоже
    реально подключен. register() присоединяет singleton `settings_router`
    к Dispatcher, а aiogram запрещает присоединять один Router к нескольким
    Dispatcher за раз — как и в проде, дispatcher строится ровно один раз
    за весь тестовый прогон.

    Возвращает объект с .dp/.bot/.llm/.vision — моки можно донастраивать
    (return_value/side_effect) в каждом тесте, пересоздавать их не нужно.
    """
    from types import SimpleNamespace
    from app.core import Core, Services

    bot = MagicMock()
    llm = MagicMock()
    vision = MagicMock()
    services = Services(bot=bot, llm=llm, vision=vision, proactive=MagicMock())
    dp = Core()._build_dispatcher(services)
    return SimpleNamespace(dp=dp, bot=bot, llm=llm, vision=vision)


def get_handler(dp, name: str):
    """Достаёт функцию-хендлер по имени из зарегистрированного Dispatcher."""
    for observer in (dp.message, dp.callback_query):
        for o in observer.handlers:
            if o.callback.__name__ == name:
                return o.callback
    raise ValueError(f"handler {name!r} not found")

