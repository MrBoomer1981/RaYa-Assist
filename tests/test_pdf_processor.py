"""
test_pdf_processor.py — extract_text() не должна течь fitz.Document.

Регрессия: extract_text() открывала fitz.open(...) и никогда не вызывала
.close() — в отличие от повторного open() чуть ниже в process() (там для
чтения количества страниц), который аккуратно закрывается. PyMuPDF
подчищает документ через __del__ как страховку, но не сразу и не
детерминированно — на каждый обработанный PDF висел лишний открытый
документ до срабатывания сборщика мусора.
"""
import fitz
import pytest

from deeper.services.pdf_processor import PDFProcessor


def _make_test_pdf(text: str = "Sample text for extraction check.") -> bytes:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), text)
    pdf_bytes = doc.tobytes()
    doc.close()
    return pdf_bytes


@pytest.fixture
def processor():
    return PDFProcessor(groq_api_key="test-key", primary_model="llama-3.3-70b-versatile")


def test_extract_text_closes_document(processor, monkeypatch):
    pdf_bytes = _make_test_pdf("Document close check.")

    opened_docs = []
    real_open = fitz.open

    def tracking_open(*a, **kw):
        doc = real_open(*a, **kw)
        opened_docs.append(doc)
        return doc

    monkeypatch.setattr(fitz, "open", tracking_open)

    text = processor.extract_text(pdf_bytes)

    assert "Document close check" in text
    assert len(opened_docs) == 1
    assert opened_docs[0].is_closed, "fitz.Document не был закрыт после extract_text()"


def test_extract_text_closes_document_even_on_error(processor, monkeypatch):
    """Документ должен закрываться и при исключении в процессе извлечения — не только на happy path."""
    pdf_bytes = _make_test_pdf()

    opened_docs = []
    real_open = fitz.open

    def tracking_open(*a, **kw):
        doc = real_open(*a, **kw)
        opened_docs.append(doc)
        return doc

    monkeypatch.setattr(fitz, "open", tracking_open)

    def broken_get_text(*a, **kw):
        raise RuntimeError("симулированная ошибка извлечения")

    # Патчим get_text на самой странице через Page-класс
    monkeypatch.setattr(fitz.Page, "get_text", broken_get_text)

    with pytest.raises(RuntimeError):
        processor.extract_text(pdf_bytes)

    assert len(opened_docs) == 1
    assert opened_docs[0].is_closed, "документ должен закрываться даже при исключении"


def test_extract_text_returns_correct_content(processor):
    pdf_bytes = _make_test_pdf("Unique phrase for content verification.")
    text = processor.extract_text(pdf_bytes)
    assert "Unique phrase for content verification" in text
