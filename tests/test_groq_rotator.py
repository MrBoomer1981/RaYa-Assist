"""
test_groq_rotator.py — ретраи и backoff при рейт-лимитах Groq.

До этого файла у groq_rotator.py не было ни одного теста — при том, что
именно его retry-логика была корнем бага "диппер превышает лимит времени
и не может найти факты" (см. историю: раньше при 429 ключи перебирались
за ~0.3с без единой реальной паузы, после чего запрос просто падал —
_analyze_chunk получал None почти на каждый чанк).

Тесты используют РЕАЛЬНЫЙ (но короткий — доли секунды) asyncio.sleep,
а не мок, и проверяют факт ожидания по настенному времени (time.monotonic).
Это чуть медленнее, чем мокать sleep, но исключает риск того, что тест
"зелёный", а реальный await asyncio.sleep(...) в коде на самом деле не
вызывается.
"""
import time

import pytest
from unittest.mock import MagicMock

from deeper.services.groq_rotator import GroqKeyRotator


class _FakeMessage:
    def __init__(self, content: str):
        self.content = content


class _FakeChoice:
    def __init__(self, content: str):
        self.message = _FakeMessage(content)


class _FakeResponse:
    def __init__(self, content: str):
        self.choices = [_FakeChoice(content)]


def _rate_limit_error(wait_hint: float) -> Exception:
    """Воспроизводит реальный формат ошибки Groq из лога пользователя."""
    return Exception(
        "Error code: 429 - {'error': {'message': 'Rate limit reached for model "
        "`llama-3.1-8b-instant` in organization `org_01kjzmmnpwerys4x1jky17z5zb` "
        "service tier `on_demand` on tokens per minute (TPM): Limit 6000, Used 5878, "
        f"Requested 1130. Please try again in {wait_hint}s. Visit "
        "https://console.groq.com/docs/rate-limits for more information.'}}"
    )


@pytest.fixture
def rotator(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key-1")
    monkeypatch.setenv("GROQ_API_KEY_2", "test-key-2")
    monkeypatch.setenv("GROQ_API_KEY_3", "test-key-3")
    r = GroqKeyRotator()
    assert len(r.clients) == 3
    return r


# ── _parse_wait_seconds ───────────────────────────────────────────────────────

def test_parse_wait_seconds_uses_groq_hint_from_real_error():
    """Реальный текст ошибки из лога пользователя: 'Please try again in 10.08s'."""
    wait = GroqKeyRotator._parse_wait_seconds(
        "... Please try again in 10.08s. ...", attempt=0
    )
    assert 10.5 <= wait <= 10.6  # +0.5с буфер из кода


def test_parse_wait_seconds_no_hint_falls_back_to_exponential_backoff():
    assert GroqKeyRotator._parse_wait_seconds("429 Too Many Requests", attempt=0) == 1.0
    assert GroqKeyRotator._parse_wait_seconds("429 Too Many Requests", attempt=1) == 2.0
    assert GroqKeyRotator._parse_wait_seconds("429 Too Many Requests", attempt=2) == 4.0


def test_parse_wait_seconds_capped_at_max_wait():
    """Даже если Groq подскажет огромное время — не ждём дольше потолка (60с)."""
    wait = GroqKeyRotator._parse_wait_seconds("Please try again in 999s.", attempt=0)
    assert wait == 60.0


# ── chat() — happy path ────────────────────────────────────────────────────────

async def test_successful_call_no_wait_no_rotation(rotator):
    rotator.clients[0].chat.completions.create = MagicMock(
        return_value=_FakeResponse("привет")
    )

    started = time.monotonic()
    result = await rotator.chat(model="llama-3.1-8b-instant", messages=[{"role": "user", "content": "hi"}])
    elapsed = time.monotonic() - started

    assert result == "привет"
    assert elapsed < 0.5  # не ждали вообще
    assert rotator._index == 0  # ротации не было — успех с первого раза


# ── chat() — рейт-лимит ──────────────────────────────────────────────────────

async def test_rate_limit_then_success_actually_waits_hinted_time(rotator):
    """
    Ключевой тест на сам фикс: раньше при 429 код ждал фиксированные 0.1с
    и сразу ротировал ключ (бесполезно — лимит на организацию, не на ключ).
    Теперь должен реально ждать время, подсказанное Groq.
    """
    calls = {"n": 0}

    def flaky_create(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _rate_limit_error(wait_hint=0.3)
        return _FakeResponse("ответ после ретрая")

    for c in rotator.clients:
        c.chat.completions.create = MagicMock(side_effect=flaky_create)

    started = time.monotonic()
    result = await rotator.chat(model="llama-3.1-8b-instant", messages=[{"role": "user", "content": "hi"}])
    elapsed = time.monotonic() - started

    assert result == "ответ после ретрая"
    assert calls["n"] == 2
    # 0.3с из подсказки + 0.5с буфер = ~0.8с. Раньше здесь было бы ~0.1с.
    assert elapsed >= 0.75, f"должны были реально подождать ~0.8с, а прошло {elapsed:.2f}с"
    assert rotator._index == 1  # ротация произошла один раз, после ожидания


async def test_rotates_through_all_keys_on_sustained_rate_limit(rotator):
    """Устойчивый 429 на всех ключах — перебираем их по кругу, а не залипаем на одном."""
    for c in rotator.clients:
        c.chat.completions.create = MagicMock(side_effect=_rate_limit_error(wait_hint=0.05))

    with pytest.raises(RuntimeError):
        await rotator.chat(
            model="llama-3.1-8b-instant",
            messages=[{"role": "user", "content": "hi"}],
            retries=4,
        )

    # 4 попытки при 3 ключах: 0→1→2→0, т.е. после 4 неудач стоим на индексе 1
    assert rotator._index == 1


async def test_all_retries_exhausted_raises_clear_runtime_error(rotator):
    for c in rotator.clients:
        c.chat.completions.create = MagicMock(side_effect=_rate_limit_error(wait_hint=0.02))

    with pytest.raises(RuntimeError, match=r"Groq keys exhausted after 3 attempts"):
        await rotator.chat(
            model="llama-3.1-8b-instant",
            messages=[{"role": "user", "content": "hi"}],
            retries=3,
        )


async def test_non_rate_limit_error_raises_immediately_without_waiting(rotator):
    """
    Ошибка НЕ про рейт-лимит (например, 500 или невалидный запрос) не должна
    ни ждать, ни ротировать ключи — это бессмысленно и только теряет время.
    """
    rotator.clients[0].chat.completions.create = MagicMock(
        side_effect=Exception("500 Internal Server Error")
    )

    started = time.monotonic()
    with pytest.raises(Exception, match="500 Internal Server Error"):
        await rotator.chat(model="llama-3.1-8b-instant", messages=[{"role": "user", "content": "hi"}])
    elapsed = time.monotonic() - started

    assert elapsed < 0.2  # ни разу не ждали
    assert rotator._index == 0  # и не ротировали


async def test_default_retries_is_more_generous_than_key_count(rotator):
    """
    Раньше retries по умолчанию = len(clients) (обычно 3) — этого хватало
    на рывок по всем ключам за доли секунды без единой реальной паузы.
    Теперь попытки дороже (реальное ожидание), бюджет больше.
    """
    attempts = {"n": 0}

    def counting_create(*a, **kw):
        attempts["n"] += 1
        raise _rate_limit_error(wait_hint=0.01)

    for c in rotator.clients:
        c.chat.completions.create = MagicMock(side_effect=counting_create)

    with pytest.raises(RuntimeError):
        await rotator.chat(model="llama-3.1-8b-instant", messages=[{"role": "user", "content": "hi"}])

    assert attempts["n"] >= 5  # max(len(clients), 5) — больше, чем просто число ключей
