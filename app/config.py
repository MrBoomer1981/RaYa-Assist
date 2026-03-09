import logging
from pathlib import Path
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

# Путь к файлу личности — лежит рядом с main.py
PERSONA_PATH = Path("persona.txt")

_DEFAULT_PERSONA = (
    "Ты мой личный ИИ-ассистент RaYa. Общаемся как старые друзья — без формальностей. "
    "Ты прямой, честный и не боишься сказать если я не прав. "
    "Помогаешь с любыми задачами: работа, идеи, тексты, планы. "
    "Отвечаешь кратко и по делу."
)


def _load_persona() -> str:
    """
    Загружает личность бота из persona.txt.
    Если файл не найден — использует дефолтный промпт.
    """
    if PERSONA_PATH.exists():
        text = PERSONA_PATH.read_text(encoding="utf-8").strip()
        if text:
            logger.info("✅ Личность загружена из %s (%d символов)", PERSONA_PATH, len(text))
            return text
        logger.warning("⚠️ %s пустой — используется дефолтный промпт", PERSONA_PATH)
    else:
        logger.info("ℹ️ %s не найден — используется дефолтный промпт", PERSONA_PATH)
    return _DEFAULT_PERSONA


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

    # Список разрешённых Telegram user_id (безопасность)
    # Формат в .env: ALLOWED_USER_IDS=123456789,987654321
    allowed_user_ids: str = ""

    # Опциональные ключи для расширений
    tavily_api_key: str = ""
    telegram_user_id: int = 1h



    # Параметры модели
    model_name: str = "llama-3.3-70b-versatile"
    temperature: float = 0.7
    max_history: int = 20

    # Личность — загружается из persona.txt, не из .env
    system_prompt: str = ""

    def model_post_init(self, __context) -> None:
        """Загружаем persona.txt после инициализации настроек."""
        if not self.system_prompt:
            object.__setattr__(self, "system_prompt", _load_persona())

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

    @property
    def allowed_ids(self) -> set[int]:
        """Возвращает множество разрешённых user_id."""
        if not self.allowed_user_ids:
            return set()
        ids = set()
        for part in self.allowed_user_ids.split(","):
            part = part.strip()
            if part.isdigit():
                ids.add(int(part))
        return ids

    @property
    def security_enabled(self) -> bool:
        return bool(self.allowed_user_ids)


settings = Settings()
