"""
test_database.py — CRUD-операции и регрессионные тесты для найденных багов.
"""
from datetime import datetime, timedelta



# ── Пользователи и имя для обращения ──────────────────────────────────────────

def test_get_user_name_priority_username_over_first_name(temp_db):
    """@username имеет приоритет над first_name — так задумано для обращения по нику."""
    temp_db.upsert_user(1, first_name="Иван", last_name="", username="ivan_the_dev")
    assert temp_db.get_user_name(1) == "ivan_the_dev"


def test_get_user_name_falls_back_to_first_name(temp_db):
    temp_db.upsert_user(2, first_name="Мария", last_name="", username="")
    assert temp_db.get_user_name(2) == "Мария"


def test_get_user_name_unknown_user_returns_default(temp_db):
    assert temp_db.get_user_name(999999) == "друг"


def test_upsert_user_then_invalidate_cache_picks_up_new_username(temp_db):
    """Пользователь завёл @username после первого сообщения — кэш должен обновиться."""
    temp_db.upsert_user(3, first_name="Пётр", last_name="", username="")
    assert temp_db.get_user_name(3) == "Пётр"

    temp_db.upsert_user(3, first_name="Пётр", last_name="", username="petya99")
    temp_db.invalidate_user_name_cache(3)
    assert temp_db.get_user_name(3) == "petya99"


# ── schedule_photo — регрессия критического бага (таблица не создавалась) ────

def test_schedule_photo_roundtrip(temp_db):
    """
    Регрессия: таблица schedule_photo раньше нигде не создавалась в init_db(),
    из-за чего save/get_schedule_photo падали с 'no such table' на любой
    свежей базе — то есть /schedule был сломан у каждого нового пользователя.
    """
    temp_db.save_schedule_photo(1, "Пн: физика 9:00\nВт: химия 10:00", "Расписание на неделю")
    sched = temp_db.get_schedule_photo(1)
    assert sched is not None
    assert sched["raw_text"] == "Пн: физика 9:00\nВт: химия 10:00"
    assert sched["summary"] == "Расписание на неделю"


def test_schedule_photo_missing_returns_none(temp_db):
    assert temp_db.get_schedule_photo(42) is None


def test_schedule_photo_upsert_replaces_previous(temp_db):
    """Одно активное расписание на пользователя — новое фото заменяет старое."""
    temp_db.save_schedule_photo(1, "старое расписание")
    temp_db.save_schedule_photo(1, "новое расписание")
    sched = temp_db.get_schedule_photo(1)
    assert sched["raw_text"] == "новое расписание"


def test_delete_schedule_photo(temp_db):
    temp_db.save_schedule_photo(1, "расписание")
    temp_db.delete_schedule_photo(1)
    assert temp_db.get_schedule_photo(1) is None


# ── Задачи ────────────────────────────────────────────────────────────────────

def test_save_and_get_active_tasks(temp_db):
    temp_db.save_task(1, "Сдать отчёт", priority=1, due_date="2026-07-10")
    temp_db.save_task(1, "Купить молоко", priority=4)
    tasks = temp_db.get_active_tasks(1)
    texts = [t[1] for t in tasks]
    assert "Сдать отчёт" in texts
    assert "Купить молоко" in texts


def test_mark_task_done_removes_from_active(temp_db):
    task_id = temp_db.save_task(1, "Разовая задача", priority=2)
    temp_db.mark_task_done(task_id, 1)
    active = temp_db.get_active_tasks(1)
    assert all(t[0] != task_id for t in active)


def test_delete_task(temp_db):
    task_id = temp_db.save_task(1, "Удалить меня", priority=2)
    temp_db.delete_task(task_id, 1)
    active = temp_db.get_active_tasks(1)
    assert all(t[0] != task_id for t in active)


def test_tasks_are_isolated_per_user(temp_db):
    temp_db.save_task(1, "Задача юзера 1", priority=2)
    temp_db.save_task(2, "Задача юзера 2", priority=2)
    tasks_1 = temp_db.get_active_tasks(1)
    tasks_2 = temp_db.get_active_tasks(2)
    assert all(t[1] != "Задача юзера 2" for t in tasks_1)
    assert all(t[1] != "Задача юзера 1" for t in tasks_2)


# ── События календаря ─────────────────────────────────────────────────────────

def test_create_and_get_events_for_day(temp_db):
    from app.calendar_service import create_event
    today = datetime.now().strftime("%Y-%m-%d")
    create_event(1, today, "Встреча с командой", time_start="10:00", time_end="11:00")
    year, month = (int(x) for x in today.split("-")[:2])
    events = temp_db.get_events_for_month(1, year, month)
    assert any(e["title"] == "Встреча с командой" for e in events)


def test_update_event(temp_db):
    from app.calendar_service import create_event
    today = datetime.now().strftime("%Y-%m-%d")
    event = create_event(1, today, "Старое название", time_start="09:00", time_end="10:00")
    ok = temp_db.update_event(event["id"], 1, title="Новое название")
    assert ok is True
    events = temp_db.get_upcoming_events(1, limit=10)
    assert any(e["title"] == "Новое название" for e in events)


def test_delete_event(temp_db):
    from app.calendar_service import create_event
    today = datetime.now().strftime("%Y-%m-%d")
    event = create_event(1, today, "Удалить", time_start="09:00", time_end="10:00")
    ok = temp_db.delete_event(event["id"], 1)
    assert ok is True
    events = temp_db.get_upcoming_events(1, limit=10)
    assert all(e["id"] != event["id"] for e in events)


# ── Напоминания ───────────────────────────────────────────────────────────────

def test_save_and_get_active_reminders(temp_db):
    remind_at = datetime.now() + timedelta(hours=1)
    rid = temp_db.save_reminder(1, "Позвонить врачу", remind_at)
    reminders = temp_db.get_active_reminders(1)
    assert any(r[0] == rid and r[1] == "Позвонить врачу" for r in reminders)


# ── Дневник ───────────────────────────────────────────────────────────────────

def test_save_and_load_diary_entry(temp_db):
    temp_db.save_diary_entry(1, "Сегодня был хороший день", mood="радость")
    entries = temp_db.load_diary_entries(1, limit=5)
    assert any("хороший день" in e[1] for e in entries)


# ── Память (user_memory legacy) ───────────────────────────────────────────────

def test_save_and_load_memory(temp_db):
    temp_db.save_memory(1, ["Любит кофе", "Живёт в Москве"])
    facts = temp_db.load_memory(1)
    assert "Любит кофе" in facts
    assert "Живёт в Москве" in facts


def test_clear_memory(temp_db):
    temp_db.save_memory(1, ["Факт для удаления"])
    temp_db.clear_memory(1)
    assert temp_db.load_memory(1) == []


# ── История диалога ───────────────────────────────────────────────────────────

def test_save_and_load_history(temp_db):
    temp_db.save_messages(1, "Привет", "Привет! Как дела?")
    history = temp_db.load_history(1, limit=10)
    assert len(history) == 2
    assert history[0].content == "Привет"
    assert history[1].content == "Привет! Как дела?"


def test_clear_history(temp_db):
    temp_db.save_messages(1, "Сообщение", "Ответ")
    temp_db.clear_history(1)
    assert temp_db.load_history(1) == []


# ── Knowledge cache ───────────────────────────────────────────────────────────

def test_knowledge_cache_set_and_get(temp_db):
    temp_db.kc_set("квантовые компьютеры", "Развёрнутый результат поиска", ttl_hours=1.0)
    cached = temp_db.kc_get("квантовые компьютеры")
    assert cached == "Развёрнутый результат поиска"


def test_knowledge_cache_expired_returns_none(temp_db):
    temp_db.kc_set("устаревший запрос", "результат", ttl_hours=-1.0)
    assert temp_db.kc_get("устаревший запрос") is None
