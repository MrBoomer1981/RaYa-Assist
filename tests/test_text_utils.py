"""
test_text_utils.py — деление текста на чанки.

test_token_chunk_never_hangs_when_overlap_exceeds_chunk_size — регрессия:
если overlap >= chunk_size, `start` в цикле _token_chunk никогда бы не
продвинулся вперёд — бесконечный цикл. Сейчас реальный конфиг безопасен
(chunk_overlap=150 < chunk_size=800 в deeper/config.py), но сама функция
никак это не гарантировала.

Примечание: tiktoken.get_encoding() качает BPE-таблицу с внешнего домена
при первом использовании — в песочнице сети нет (не в whitelist), поэтому
тесты подменяют _get_encoder() на простой фейковый энкодер (1 символ =
1 "токен"), чтобы проверять именно ЛОГИКУ цикла, а не тянуть сеть.
Тест на зависание всё равно запускает вызов в отдельном потоке с
таймаутом — на случай, если фикс когда-нибудь всё же сломают.
"""
import threading

import deeper.utils.text_utils as tu


class _FakeEncoder:
    """1 символ = 1 токен — достаточно для проверки логики цикла чанкинга."""
    def encode(self, text: str) -> list:
        return list(text)

    def decode(self, tokens: list) -> str:
        return "".join(tokens)


def _use_fake_encoder(monkeypatch):
    monkeypatch.setattr(tu, "_get_encoder", lambda: _FakeEncoder())
    monkeypatch.setattr(tu, "count_tokens", lambda text: len(text))


def test_chunk_text_basic_splits_on_paragraphs(monkeypatch):
    _use_fake_encoder(monkeypatch)
    text = "Первый абзац с текстом.\n\nВторой абзац с другим текстом.\n\nТретий абзац."
    chunks = tu.chunk_text(text, chunk_size=800, overlap=150)
    assert len(chunks) >= 1
    assert "Первый абзац" in "".join(chunks)
    assert "Третий абзац" in "".join(chunks)


def test_chunk_text_oversized_single_block_gets_split(monkeypatch):
    _use_fake_encoder(monkeypatch)
    long_text = "слово " * 500  # 3000 символов = 3000 "токенов" у фейкового энкодера
    chunks = tu.chunk_text(long_text, chunk_size=100, overlap=20)
    assert len(chunks) > 1
    for c in chunks:
        assert len(c) <= 100


def test_token_chunk_never_hangs_when_overlap_equals_chunk_size(monkeypatch):
    """
    Патологическая конфигурация (overlap == chunk_size) не должна вешать
    процесс. Раньше — вешала бы. Запускаем в отдельном потоке с таймаутом,
    чтобы тест сам не завис, если фикс когда-нибудь сломают.
    """
    _use_fake_encoder(monkeypatch)
    long_text = "x" * 2000
    result = {}

    def run():
        result["chunks"] = tu._token_chunk(long_text, chunk_size=50, overlap=50)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout=10)

    assert not t.is_alive(), "_token_chunk зависла при overlap == chunk_size"
    assert len(result["chunks"]) > 0


def test_token_chunk_overlap_greater_than_chunk_size_also_safe(monkeypatch):
    _use_fake_encoder(monkeypatch)
    long_text = "x" * 2000
    result = {}

    def run():
        result["chunks"] = tu._token_chunk(long_text, chunk_size=50, overlap=200)  # overlap > chunk_size

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout=10)

    assert not t.is_alive(), "_token_chunk зависла при overlap > chunk_size"
    assert len(result["chunks"]) > 0


def test_token_chunk_normal_config_step_unaffected_by_fix(monkeypatch):
    """Реальные пропорции из deeper/config.py (overlap < chunk_size) — шаг цикла не изменился."""
    _use_fake_encoder(monkeypatch)
    long_text = "x" * 500
    chunks = tu._token_chunk(long_text, chunk_size=100, overlap=20)
    assert len(chunks) >= 2
    # Шаг должен быть chunk_size - overlap = 80, не 1 (защита не должна включаться зря)
    assert len(chunks[0]) == 100
