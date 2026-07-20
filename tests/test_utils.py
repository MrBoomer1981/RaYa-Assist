"""
test_utils.py — общие утилиты: utcnow(), strip_json(), clean_reply().
"""
from datetime import datetime, timedelta

from app.utils import clean_reply, strip_json, utcnow


def test_utcnow_returns_naive_datetime():
    """
    Критично: должен быть НАИВНЫМ (без tzinfo), иначе сравнение с датами,
    распарсенными из SQLite (тоже наивными), упадёт с TypeError.
    """
    now = utcnow()
    assert isinstance(now, datetime)
    assert now.tzinfo is None


def test_utcnow_compatible_with_naive_datetime_arithmetic():
    now = utcnow()
    other = datetime.strptime("2020-01-01 00:00:00", "%Y-%m-%d %H:%M:%S")
    diff = now - other  # не должно кидать TypeError (naive - naive OK)
    assert diff > timedelta(days=0)


def test_utcnow_is_close_to_real_utc_time():
    import datetime as dt_module
    now = utcnow()
    real_utc = dt_module.datetime.now(dt_module.timezone.utc).replace(tzinfo=None)
    assert abs((now - real_utc).total_seconds()) < 5


# ── now_msk() / today_msk_str() ───────────────────────────────────────────────
# Регрессия: несколько мест в коде (calendar_agent, diary_agent, morning_agent)
# считали "сегодня" через utcnow().date()/.strftime(...) напрямую, без
# поправки на МСК. В окне UTC 21:00-23:59 (МСК уже 00:00-02:59 следующих
# суток) это молча давало вчерашнюю дату. now_msk() — общее место для этой
# поправки, чтобы больше не размножать её вручную по файлам.

def test_now_msk_is_three_hours_ahead_of_utc():
    from app.utils import now_msk
    diff = (now_msk() - utcnow()).total_seconds()
    assert abs(diff - 3 * 3600) < 5


def test_now_msk_rolls_date_over_at_msk_midnight(monkeypatch):
    """22:30 UTC вторник = 01:30 МСК среда — дата должна перейти на следующие сутки."""
    import app.utils as utils_mod

    fake_utc = datetime(2026, 7, 14, 22, 30)  # вторник
    monkeypatch.setattr(utils_mod, "utcnow", lambda: fake_utc)

    result = utils_mod.now_msk()
    assert result == datetime(2026, 7, 15, 1, 30)  # среда, 01:30
    assert result.date().isoformat() == "2026-07-15"


def test_today_msk_str_format(monkeypatch):
    import app.utils as utils_mod

    fake_utc = datetime(2026, 7, 14, 10, 0)
    monkeypatch.setattr(utils_mod, "utcnow", lambda: fake_utc)

    assert utils_mod.today_msk_str() == "2026-07-14"


def test_strip_json_removes_markdown_fences():
    raw = '```json\n{"a": 1}\n```'
    assert strip_json(raw) == '{"a": 1}'


def test_strip_json_passes_through_plain_json():
    raw = '{"a": 1}'
    assert strip_json(raw) == '{"a": 1}'


def test_clean_reply_strips_whitespace():
    assert clean_reply("  привет  ") == "привет"
