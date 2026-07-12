"""
test_core.py — сборка Dispatcher в Core: middleware должен покрывать
и message, и callback_query.

Регрессия: AccessMiddleware был подключен только к dp.message — после того
как владелец задаёт OWNER_USER_ID, нажатия на inline-кнопки (например,
выбор режима /deeper) оставались вообще без проверки доступа.
"""
from app.middleware import AccessMiddleware


def test_access_middleware_covers_messages(registered_dispatcher):
    dp = registered_dispatcher.dp
    assert any(isinstance(m, AccessMiddleware) for m in dp.message.middleware)


def test_access_middleware_covers_callback_queries(registered_dispatcher):
    """Регрессия: раньше нажатия на inline-кнопки не проверялись вообще."""
    dp = registered_dispatcher.dp
    assert any(isinstance(m, AccessMiddleware) for m in dp.callback_query.middleware)
