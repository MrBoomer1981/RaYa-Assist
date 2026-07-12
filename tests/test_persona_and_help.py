"""
test_persona_and_help.py — обращение по нику, актуальность /help.
"""
from pathlib import Path

from app.agents.raya_agent import _build_hard_rules
from app.config import settings
from app.handlers import _build_help_text


def test_persona_has_no_hardcoded_name():
    """
    Регрессия: persona.txt раньше десятки раз хардкодил 'Сократ' и прямо
    запрещал обращаться иначе — это перебивало динамическую подстановку ника.
    """
    persona_path = Path(__file__).resolve().parent.parent / "persona.txt"
    text = persona_path.read_text(encoding="utf-8")
    assert "Сократ" not in text
    assert "vault" not in text.lower()


def test_dynamic_nickname_is_not_overridden_by_persona():
    rules = _build_hard_rules("nikita_dev")
    full_prompt = settings.system_prompt + "\n\n" + rules
    assert "Сократ" not in full_prompt
    assert "nikita_dev" in full_prompt


def test_help_text_mentions_deeper_command(monkeypatch):
    monkeypatch.setattr(settings, "tavily_api_key", "tvly-fake-for-test")
    help_text = _build_help_text()
    assert "/deeper" in help_text


def test_help_text_has_no_vault_command():
    help_text = _build_help_text()
    assert "/vault" not in help_text
    assert "obsidian" not in help_text.lower()
