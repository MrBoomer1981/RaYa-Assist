"""
test_router_calibration.py — RouterCalibration не должна течь соединениями.

Регрессия: init_calibration_table()/get_hint()/_save_feedback() открывали
`with sqlite3.connect(...) as con:` напрямую. Для sqlite3.Connection
контекстный менеджер управляет ТОЛЬКО commit/rollback транзакции и
НЕ закрывает само соединение — задокументированное, но частое заблуждение
о поведении Python. get_hint() вызывается на КАЖДОМ сообщении в чате
(см. app/llm_service.py) — то есть каждое сообщение утекало одним
sqlite3-соединением/файловым дескриптором. На длинной дистанции (Railway,
процесс работает днями/неделями) это вело бы к исчерпанию дескрипторов
и труднодиагностируемым сбоям "через какое-то время работы".

Фикс — переиспользовать app.database._conn(), который реально закрывает
соединение в finally.

Примечание про тестирование: sqlite3.Connection — C-level тип, его
атрибуты (в т.ч. .close) нельзя переопределить на инстансе — поэтому
утечку проверяем не перехватом .close(), а попыткой выполнить запрос на
перехваченном соединении ПОСЛЕ вызова: если оно закрыто — sqlite3 кинет
ProgrammingError, если утекло — запрос спокойно выполнится.
"""
import sqlite3

from app.llm_pipeline import RouterCalibration


def _install_connection_tracker(monkeypatch):
    """Перехватывает sqlite3.connect — потом используем для проверки, что все закрыты."""
    created = []
    real_connect = sqlite3.connect

    def tracking_connect(*a, **kw):
        con = real_connect(*a, **kw)
        created.append(con)
        return con

    monkeypatch.setattr(sqlite3, "connect", tracking_connect)
    return created


def _count_still_open(connections: list) -> int:
    """Сколько из перехваченных соединений всё ещё не закрыты (т.е. утекли)."""
    still_open = 0
    for con in connections:
        try:
            con.execute("SELECT 1")
            still_open += 1
        except sqlite3.ProgrammingError:
            pass  # закрыто — как и должно быть
    return still_open


def test_init_calibration_table_does_not_leak_connection(temp_db, monkeypatch):
    created = _install_connection_tracker(monkeypatch)

    RouterCalibration()  # __init__ вызывает init_calibration_table()

    assert len(created) >= 1
    leaked = _count_still_open(created)
    assert leaked == 0, f"{leaked} из {len(created)} соединений не закрыты"


def test_get_hint_does_not_leak_connections_across_many_calls(temp_db, monkeypatch):
    """Главный сценарий бага: get_hint() дёргается на каждое сообщение в чате."""
    calib = RouterCalibration()

    created = _install_connection_tracker(monkeypatch)
    for i in range(10):
        calib.get_hint(f"случайное сообщение номер {i} про питон и базы данных")

    assert len(created) >= 10
    leaked = _count_still_open(created)
    assert leaked == 0, f"утечка после {len(created)} вызовов get_hint: {leaked} не закрыто"


def test_check_mismatch_save_feedback_does_not_leak(temp_db, monkeypatch):
    calib = RouterCalibration()
    calib.record_route(1, "покажи погоду", "weather")

    created = _install_connection_tracker(monkeypatch)
    result = calib.check_mismatch(1, "я не про это спрашивал, имел в виду другое")

    assert result is True  # сигнал недовольства распознан → фидбэк сохранён
    assert len(created) >= 1
    assert _count_still_open(created) == 0


def test_get_hint_still_works_functionally_after_refactor(temp_db):
    """Не только 'не течёт', но и продолжает делать то, что должна."""
    calib = RouterCalibration()
    calib.record_route(1, "покажи погоду в Москве", "weather")
    calib.check_mismatch(1, "не про это спрашивал, я имел в виду другое")
    calib.check_mismatch(1, "не про это спрашивал, я имел в виду другое")

    # То же словоупотребление, что в prev_msg — get_hint матчит по сырой
    # подстроке слова, без лемматизации (отдельная, некритичная особенность
    # эвристики — не предмет этого фикса).
    hint = calib.get_hint("покажи погоду в Москве")
    assert hint is not None
    assert "weather" in hint
