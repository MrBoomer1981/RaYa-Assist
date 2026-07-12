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


def test_strip_json_removes_markdown_fences():
    raw = '```json\n{"a": 1}\n```'
    assert strip_json(raw) == '{"a": 1}'


def test_strip_json_passes_through_plain_json():
    raw = '{"a": 1}'
    assert strip_json(raw) == '{"a": 1}'


def test_clean_reply_strips_whitespace():
    assert clean_reply("  привет  ") == "привет"
