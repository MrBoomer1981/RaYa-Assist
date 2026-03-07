from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Конфигурация приложения — все параметры берутся из .env файла."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Обязательные ключи
    groq_api_key: str
    telegram_token: str

    # Опциональные ключи для расширений
    tavily_api_key: str = ""

    # Параметры модели
    model_name: str = "llama-3.3-70b-versatile"
    temperature: float = 0.7
    max_history: int = 20

    # Личность бота
    system_prompt: str = (
        "Ты мой личный ИИ-ассистент RaYa. Общаемся как старые друзья — без формальностей. "
        "Ты прямой, честный и не боишься сказать если я не прав. "
        "Помогаешь с любыми задачами: работа, идеи, тексты, планы. "
        "Отвечаешь кратко и по делу."
    )

    @field_validator("temperature")
    @classmethod
    def validate_temperature(cls, v: float) -> float:
        if not 0.0 <= v <= 2.0:
            raise ValueError("temperature должна быть от 0.0 до 2.0")
        return v

    @field_validator("max_history")
    @classmethod
    def validate_max_history(cls, v: int) -> int:
        if v < 2:
            raise ValueError("max_history должен быть минимум 2")
        return v

    @property
    def search_enabled(self) -> bool:
        return bool(self.tavily_api_key)


settings = Settings()
