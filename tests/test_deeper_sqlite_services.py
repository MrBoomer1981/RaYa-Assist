"""
test_deeper_sqlite_services.py — KnowledgeBase / MemoryService / CacheManager
не должны течь соединениями.

Тот же класс бага, что чинили в app/llm_pipeline.py::RouterCalibration и
app/personality_service.py::update_emotional_patterns (см. соответствующие
тестовые файлы): `_connect()` возвращала голый sqlite3.Connection,
используемый как `with self._connect() as conn:` — для sqlite3.Connection
это управляет ТОЛЬКО commit/rollback транзакции, но НЕ закрывает само
соединение. Здесь паттерн был скопирован в ТРИ разных файла одинаково,
и это особенно опасно: методы этих классов вызываются МНОГОКРАТНО за один
запуск deep research (на каждый URL при скрейпинге — cache_manager, на
каждое сообщение в диалоге с DEEper — memory, на каждое сохранение
исследования — knowledge_base), так что один долгий research мог утекать
десятками файловых дескрипторов разом.
"""
import sqlite3


from deeper.services.cache_manager import CacheManager
from deeper.services.knowledge_base import KnowledgeBase
from deeper.services.memory import MemoryService


def _count_still_open(connections: list) -> int:
    still_open = 0
    for con in connections:
        try:
            con.execute("SELECT 1")
            still_open += 1
        except sqlite3.ProgrammingError:
            pass
    return still_open


def _install_tracker(monkeypatch):
    created = []
    real_connect = sqlite3.connect

    def tracking_connect(*a, **kw):
        con = real_connect(*a, **kw)
        created.append(con)
        return con

    monkeypatch.setattr(sqlite3, "connect", tracking_connect)
    return created


# ── KnowledgeBase ──────────────────────────────────────────────────────────

async def test_knowledge_base_save_and_get_does_not_leak(tmp_path, monkeypatch):
    kb = KnowledgeBase(str(tmp_path / "kb.db"))

    created = _install_tracker(monkeypatch)
    research_id = await kb.save_research(
        title="Тест", summary="Краткое содержание",
        report="Полный отчёт", sources=["https://a.com", "https://b.com"],
    )
    got = kb.get_research(research_id)
    kb.list_researches()

    assert got is not None
    assert got.title == "Тест"
    assert len(created) >= 3  # save + get + list, минимум по одному соединению каждый
    leaked = _count_still_open(created)
    assert leaked == 0, f"{leaked} из {len(created)} соединений KnowledgeBase не закрыты"


async def test_knowledge_base_enforce_limit_deletes_oldest(tmp_path):
    """Функциональная проверка заодно — commit не потерялся при рефакторинге _connect()."""
    kb = KnowledgeBase(str(tmp_path / "kb.db"), max_researches=2)

    for i in range(3):
        await kb.save_research(f"Тема {i}", "сводка", "отчёт", [])

    remaining = kb.list_researches()
    assert len(remaining) == 2
    assert all(r.title != "Тема 0" for r in remaining)  # самая старая удалена


# ── MemoryService ──────────────────────────────────────────────────────────

def test_memory_service_add_and_get_does_not_leak(tmp_path, monkeypatch):
    mem = MemoryService(str(tmp_path / "memory.db"))

    created = _install_tracker(monkeypatch)
    mem.add_message(1, "user", "привет")
    mem.add_message(1, "assistant", "привет!")
    context = mem.get_context(1)

    assert len(context) == 2
    assert len(created) >= 2
    leaked = _count_still_open(created)
    assert leaked == 0, f"{leaked} из {len(created)} соединений MemoryService не закрыты"


def test_memory_service_persists_across_calls(tmp_path):
    """commit не потерялся: сообщение реально сохраняется, не только в памяти процесса."""
    db_path = str(tmp_path / "memory.db")
    MemoryService(db_path).add_message(1, "user", "тест персистентности")

    mem2 = MemoryService(db_path)  # новый инстанс, тот же файл
    context = mem2.get_context(1)
    assert any("тест персистентности" in m["content"] for m in context)


# ── CacheManager ───────────────────────────────────────────────────────────

def test_cache_manager_set_and_get_does_not_leak(tmp_path, monkeypatch):
    cache = CacheManager(str(tmp_path / "cache.db"))

    created = _install_tracker(monkeypatch)
    cache.set("https://example.com", "содержимое страницы")
    content = cache.get("https://example.com")

    assert content == "содержимое страницы"
    assert len(created) >= 2
    leaked = _count_still_open(created)
    assert leaked == 0, f"{leaked} из {len(created)} соединений CacheManager не закрыты"


def test_cache_manager_persists_across_calls(tmp_path):
    db_path = str(tmp_path / "cache.db")
    CacheManager(db_path).set("https://persist.com", "данные")

    cache2 = CacheManager(db_path)
    assert cache2.get("https://persist.com") == "данные"
