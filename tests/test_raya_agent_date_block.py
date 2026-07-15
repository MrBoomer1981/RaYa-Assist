"""
test_raya_agent_date_block.py — регрессия: дата/день недели "отставали"
от МСК-часа в _build_date_block().

Баг: МСК-час считался как (now_utc.hour + 3) % 24, а дата и день недели
брались напрямую из now_utc — без сдвига. В окне UTC 21:00-23:59 (когда в
МСК уже следующие сутки, 00:00-02:59) час показывался верно, а дата и день
недели оставались вчерашними — бот мог сказать "сейчас вторник, 01:30",
хотя в Москве уже среда.

Найдено попутно при разборе бага "10:41 AM по московскому времени" — тот
же блок отвечает за передачу текущего времени модели.
"""
from datetime import datetime

from app.agents.raya_agent import _build_date_block


def test_date_block_rolls_over_after_msk_midnight():
    """22:30 UTC во вторник = 01:30 МСК в среду — блок должен показывать среду."""
    now_utc = datetime(2026, 7, 14, 22, 30)  # вторник, 22:30 UTC
    block = _build_date_block(now_utc)

    assert "01:30 МСК" in block
    assert "среда" in block
    assert "вторник" not in block
    assert "15 июля" in block
    assert "14 июля" not in block


def test_date_block_regular_daytime_no_rollover():
    """10:00 UTC — обычное смещение +3 без перехода через полночь."""
    now_utc = datetime(2026, 7, 14, 10, 0)  # вторник
    block = _build_date_block(now_utc)

    assert "13:00 МСК" in block
    assert "вторник" in block
    assert "14 июля" in block
