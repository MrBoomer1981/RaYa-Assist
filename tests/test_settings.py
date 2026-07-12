"""
test_settings.py — round-trip настроек, дефолты, отсутствие module_obsidian.
"""


def test_default_settings_have_no_obsidian_field(temp_settings):
    s = temp_settings.get()
    assert not hasattr(s, "module_obsidian")


def test_update_and_reload_roundtrip(temp_settings):
    ok = temp_settings.update("digest_time", "07:30")
    assert ok is True

    # Сбрасываем синглтон, чтобы проверить что значение реально ушло на диск
    temp_settings._settings = None
    reloaded = temp_settings.get()
    assert reloaded.digest_time == "07:30"


def test_update_invalid_key_returns_false(temp_settings):
    ok = temp_settings.update("no_such_setting", "value")
    assert ok is False


def test_toggle_boolean_setting(temp_settings):
    s = temp_settings.get()
    original = s.module_ideas
    temp_settings.update("module_ideas", not original)
    assert temp_settings.get().module_ideas != original


def test_reset_restores_defaults(temp_settings):
    temp_settings.update("digest_time", "23:59")
    temp_settings.reset()
    assert temp_settings.get().digest_time != "23:59"


def test_schema_has_no_obsidian_entries(temp_settings):
    """Регрессия: пункт меню Obsidian должен быть полностью убран из /settings."""
    schema_keys = [item["key"] for item in temp_settings.SETTINGS_SCHEMA]
    assert "module_obsidian" not in schema_keys
    assert not any("obsidian" in k.lower() for k in schema_keys)
