"""
config.py — ТОЛЬКО инфраструктурные секреты из .env.

Всё что меняется через /settings — в app/settings.py.
Это файл не трогается в runtime.
"""
import logging
from pathlib import Path
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


def _load_persona() -> str:
    path = Path("persona.txt")
    if path.exists():
        text = path.read_text(encoding="utf-8").strip()
        if text:
            return text
    return (
        "Ты мой личный ИИ-ассистент RaYa. Общаемся как старые друзья — без формальностей. "
        "Ты прямой, честный и не боишься сказать если я не прав. "
        "Помогаешь с любыми задачами: работа, идеи, тексты, планы. "
        "Отвечаешь кратко и по делу."
    )


class Settings(BaseSettings):
    """
    Инфраструктурные настройки — из .env / Railway Variables.
    НЕ меняются через /settings.
    Меняемые пользователем настройки — в app/settings.py.
    """
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── Обязательные ────────────────────────────────────────────────────────
    groq_api_key:    str
    telegram_token:  str

    # ── Доступ ──────────────────────────────────────────────────────────────
    owner_user_id: int = 0   # 0 = dev mode (все пользователи)

    # ── API ключи ────────────────────────────────────────────────────────────
    tavily_api_key: str = ""

    # ── GitHub vault (альтернатива Obsidian REST API) ─────────────────────────
    github_token:      str = ""
    github_vault_repo: str = ""   # формат: user/repo-name

    # ── Obsidian (Phase 3) ───────────────────────────────────────────────────
    obsidian_vault_path: str = ""
    obsidian_api_url:    str = ""
    obsidian_api_key:    str = ""

    # ── Модели (дефолты — можно перекрыть в .env) ────────────────────────────
    # Пользователь может менять температуру через /settings,
    # но имена моделей фиксированы в .env
    model_name:   str = "llama-3.3-70b-versatile"
    router_model: str = "llama-3.1-8b-instant"

    # ── Таймауты ─────────────────────────────────────────────────────────────
    agent_timeout: int = 30

    # ── Личность (из файла, не из .env) ──────────────────────────────────────
    system_prompt: str = ""

    def model_post_init(self, __context) -> None:
        if not self.system_prompt:
            object.__setattr__(self, "system_prompt", _load_persona())

    @property
    def search_enabled(self) -> bool:
        return bool(self.tavily_api_key)

    @property
    def obsidian_enabled(self) -> bool:
        return bool(
            self.obsidian_vault_path
            or self.obsidian_api_url
            or (self.github_token and self.github_vault_repo)
        )

    @property
    def obsidian_via_github(self) -> bool:
        return bool(self.github_token and self.github_vault_repo)


settings = Settings()
