"""
test_search_decision.py — регрессия бага "10:41 AM по московскому времени".

Баг: вопросы про текущее время ("сколько времени сейчас") уходили в LLM-
классификатор поиска (_decide_search), который решал, что нужен веб-поиск,
и подставлял в контекст модели случайный сниппет с чужого сайта — вместо
корректно вычисленного времени из _build_date_block(). Модели явно
указывалось доверять результатам поиска как "свежим данным", поэтому она
подставляла время с найденной страницы вместо правильно посчитанного.
Два подряд вопроса о времени в одном диалоге дали два разных и оба
неверных ответа (10:41 AM и 20:55 при реальном времени около 20:54 МСК).

Фикс: явные вопросы "сейчас время/дата" отсекаются регуляркой ДО обращения
к LLM-классификатору — поиск для них никогда не запускается, и классификатор
даже не вызывается (одна из проблем изначально — он мог отработать
непредсказуемо под рейт-лимитом, как и остальные вызовы router_model).
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.llm_service import LLMService


@pytest.fixture
def service():
    svc = LLMService()
    svc._search = MagicMock()  # эмулируем включённый поиск (Tavily)
    svc._router_llm = MagicMock(ainvoke=AsyncMock(
        return_value=MagicMock(content='{"needs_search": true, "query": "test"}')
    ))
    return svc


@pytest.mark.parametrize("message", [
    "Рай, сколько времени сейчас",
    "А сейчас сколько",
    "который час",
    "Который Час?",
    "какая сегодня дата",
    "какое сегодня число",
    "текущее время",
    "текущая дата",
    "сколько сейчас",
])
async def test_time_questions_never_trigger_search(service, message):
    needs_search, query = await service._decide_search(message)

    assert needs_search is False
    assert query == ""
    # Главное: классификатор вообще не должен вызываться. Раньше вопрос
    # уходил в LLM (router_model = llama-3.1-8b-instant), которая под
    # рейт-лимитом (см. groq_rotator.py) вела себя непредсказуемо, а без
    # рейт-лимита всё равно могла решить искать — прямой пре-фильтр это
    # исключает полностью.
    service._router_llm.ainvoke.assert_not_awaited()


async def test_non_time_question_still_goes_through_classifier(service):
    """Контрольный тест — обычные вопросы не должны попадать под фильтр времени."""
    needs_search, query = await service._decide_search("погода в Самаре завтра")

    service._router_llm.ainvoke.assert_awaited_once()
    assert needs_search is True


async def test_price_question_with_word_sejchas_not_blocked(service):
    """
    'сейчас' в вопросе о цене — не про время, поиск должен идти как обычно.
    Проверка на то, что регэксп не слишком жадный.
    """
    needs_search, query = await service._decide_search(
        "курс доллара сейчас сколько стоит"
    )

    service._router_llm.ainvoke.assert_awaited_once()
    assert needs_search is True
