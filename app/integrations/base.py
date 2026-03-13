"""
base.py — базовый интерфейс для всех интеграций.

Любая интеграция наследует BaseIntegration и реализует:
  - name: str          — уникальный идентификатор
  - setup()            — инициализация (загрузка ключей, проверка соединения)
  - teardown()         — очистка ресурсов
  - is_available()     — проверка доступности прямо сейчас

Опционально:
  - handle_event(event) — реакция на входящее событие (webhook, MQTT и т.д.)

Пример подключения в core.py:
    from app.integrations.weather import WeatherIntegration
    self._integrations = [WeatherIntegration()]
    for intg in self._integrations:
        await intg.setup()
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass
class IntegrationEvent:
    """Входящее событие от внешней системы."""
    source:  str        # имя интеграции
    kind:    str        # тип события: "sensor_reading", "calendar_alert", etc.
    payload: dict       # данные события
    user_id: int | None = None  # кому адресовано (None = широковещательное)


class BaseIntegration(ABC):
    """Базовый класс для всех интеграций с внешним миром."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Уникальный идентификатор интеграции."""

    async def setup(self) -> None:
        """Инициализация. Переопределить если нужна асинхронная настройка."""

    async def teardown(self) -> None:
        """Очистка ресурсов при остановке."""

    def is_available(self) -> bool:
        """Доступна ли интеграция прямо сейчас."""
        return True

    async def handle_event(self, event: IntegrationEvent) -> str | None:
        """
        Обработка входящего события.
        Возвращает текст который RaYa должна отправить пользователю, или None.
        """
        return None

    def __repr__(self) -> str:
        status = "✅" if self.is_available() else "❌"
        return f"{status} Integration({self.name})"
