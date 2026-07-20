"""
test_personality_service.py — update_emotional_patterns() не должна течь
соединениями.

Тот же класс бага, что был в RouterCalibration (см. test_router_calibration.py):
`with sqlite3.connect(...) as con:` не закрывает соединение — контекстный
менеджер sqlite3.Connection управляет только commit/rollback. Вызывается
каждое 6-е сообщение (app/llm_service.py) — медленнее, чем у калибровки
роутера, но тот же неограниченный по времени процесс на Railway рано или
поздно упёрся бы в исчерпание файловых дескрипторов.
"""
import sqlite3


from app.personality_service import update_emotional_patterns


def _count_still_open(connections: list) -> int:
    still_open = 0
    for con in connections:
        try:
            con.execute("SELECT 1")
            still_open += 1
        except sqlite3.ProgrammingError:
            pass
    return still_open


async def test_update_emotional_patterns_does_not_leak_connection(temp_db, monkeypatch):
    for i in range(6):
        mood = "stressed" if i % 2 == 0 else "calm"
        temp_db.save_mood(1, mood, f"контекст {i}")

    created = []
    real_connect = sqlite3.connect

    def tracking_connect(*a, **kw):
        con = real_connect(*a, **kw)
        created.append(con)
        return con

    monkeypatch.setattr(sqlite3, "connect", tracking_connect)

    await update_emotional_patterns(1)

    assert len(created) >= 1
    leaked = _count_still_open(created)
    assert leaked == 0, f"{leaked} из {len(created)} соединений не закрыты"


async def test_update_emotional_patterns_detects_stress_day(temp_db):
    """Функциональная проверка — не только 'не течёт', но и работает."""
    import app.database as db_mod
    from datetime import datetime, timedelta

    # Функция требует минимум 5 записей ВСЕГО, и минимум 2 на конкретный
    # день для оценки его как "стрессового". 3 записи "stressed" в
    # понедельник + 2 нейтральные во вторник = 5 всего, понедельник 100% стресс.
    monday  = datetime(2026, 7, 13, 10, 0)   # понедельник
    tuesday = datetime(2026, 7, 14, 10, 0)   # вторник
    with db_mod._conn() as con:
        for i in range(3):
            ts = (monday + timedelta(minutes=i)).strftime("%Y-%m-%d %H:%M:%S")
            con.execute(
                "INSERT INTO mood_log (user_id, mood, context, created_at) VALUES (?, ?, ?, ?)",
                (1, "stressed", "", ts),
            )
        for i in range(2):
            ts = (tuesday + timedelta(minutes=i)).strftime("%Y-%m-%d %H:%M:%S")
            con.execute(
                "INSERT INTO mood_log (user_id, mood, context, created_at) VALUES (?, ?, ?, ?)",
                (1, "calm", "", ts),
            )

    await update_emotional_patterns(1)

    facts = db_mod.get_memory_by_category(1, "emotional_patterns")
    assert "понедельник" in facts.get("стресс_паттерн", "")
