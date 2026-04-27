"""
web_server.py — FastAPI веб-интерфейс RaYa.
Защита через токен в URL: /?token=YOUR_TOKEN
Все разделы: чат, память, дневник, напоминания.
"""
import base64
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.config import settings
from app.tts_service import TTSService
from app.voice_service import VoiceService
from app.database import (
    MEMORY_CATEGORIES,
    clear_history,
    clear_memory,
    clear_structured_memory,
    delete_memory_entry,
    delete_reminder,
    get_active_reminders,
    get_all_known_users,
    get_conversation_context,
    get_structured_memory,
    load_diary_entries,
    load_history,
    load_memory,
    save_conversation_context,
    save_reminder,
)

logger = logging.getLogger(__name__)

STATIC_DIR  = Path("static")
_MEDIA_DIR  = Path("static/media")
_MEDIA_DIR.mkdir(parents=True, exist_ok=True)
_WEB_TOKEN: str = os.getenv("WEB_TOKEN", "")

# ── Pydantic схемы ────────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    message: str

class ReminderRequest(BaseModel):
    text: str
    remind_at: str   # "YYYY-MM-DD HH:MM:SS"


def _get_user_id() -> int:
    """
    Возвращает user_id для веб-интерфейса.
    Приоритет: TELEGRAM_USER_ID из настроек -> первый пользователь в БД.
    Если пользователей ещё нет — возвращает HTTP 503.
    """
    if settings.telegram_user_id:
        return settings.telegram_user_id
    users = get_all_known_users()
    if not users:
        raise HTTPException(
            status_code=503,
            detail="Нет пользователей. Сначала напишите боту в Telegram — он запомнит ваш ID."
        )
    return users[0]


# ── Приложение ────────────────────────────────────────────────────────────────

def create_app(llm_service) -> FastAPI:
    """
    Фабрика FastAPI приложения.
    llm_service передаётся снаружи — тот же экземпляр что использует бот.
    """
    app = FastAPI(title="RaYa", docs_url=None, redoc_url=None)
    _tts   = TTSService()
    _voice = VoiceService()

    (STATIC_DIR / "media").mkdir(parents=True, exist_ok=True)

    def _check_token(token: str = Query(default="")) -> None:
        """Проверяет токен. Если WEB_TOKEN не задан — пропускает всех."""
        if _WEB_TOKEN and token != _WEB_TOKEN:
            raise HTTPException(status_code=401, detail="Неверный токен")

    # ── Статика ───────────────────────────────────────────────────────────────

    @app.get("/")
    async def index(token: str = Query(default="")):
        _check_token(token)
        return FileResponse(STATIC_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    # ── Чат ───────────────────────────────────────────────────────────────────

    @app.post("/api/chat")
    async def chat(req: ChatRequest, token: str = Query(default="")):
        _check_token(token)
        try:
            user_id = _get_user_id()
            result = await llm_service.chat(user_id, req.message)

            image_url: Optional[str] = None
            if "image" in result.agent_name:
                image_bytes = (result.metadata or {}).get("image_bytes")
                if image_bytes:
                    import uuid
                    fname = f"{uuid.uuid4().hex}.jpg"
                    fpath = _MEDIA_DIR / fname
                    fpath.write_bytes(image_bytes)
                    image_url = f"/static/media/{fname}"
                    logger.info("Изображение сохранено: %s", fname)

            return {
                "reply":      result.reply,
                "agent_name": result.agent_name,
                "reminder":   result.reminder,
                "image_url":  image_url,
                "emotion":    (result.metadata or {}).get("emotion", "calm"),
            }
        except HTTPException:
            raise
        except Exception as e:
            logger.exception("Ошибка чата")
            raise HTTPException(status_code=500, detail=str(e))

    @app.get("/api/history")
    async def history(token: str = Query(default="")):
        _check_token(token)
        user_id = _get_user_id()
        messages = load_history(user_id, limit=50)
        return [
            {"role": "human" if m.__class__.__name__ == "HumanMessage" else "ai",
             "content": m.content}
            for m in messages
        ]

    @app.delete("/api/history")
    async def delete_history(token: str = Query(default="")):
        _check_token(token)
        user_id = _get_user_id()
        clear_history(user_id)
        return {"ok": True}

    # ── Память ────────────────────────────────────────────────────────────────

    @app.get("/api/memory")
    async def memory(token: str = Query(default="")):
        _check_token(token)
        user_id = _get_user_id()
        structured = get_structured_memory(user_id)
        legacy = load_memory(user_id)
        return {
            "structured":   structured,
            "categories":   MEMORY_CATEGORIES,
            "legacy_facts": legacy,
        }

    @app.delete("/api/memory")
    async def delete_memory(token: str = Query(default="")):
        _check_token(token)
        user_id = _get_user_id()
        clear_memory(user_id)
        clear_structured_memory(user_id)
        return {"ok": True}

    @app.delete("/api/memory/{category}/{key}")
    async def delete_memory_entry_route(
        category: str, key: str,
        token: str = Query(default=""),
    ):
        """Удаляет конкретную запись из структурированной памяти."""
        _check_token(token)
        user_id = _get_user_id()
        ok = delete_memory_entry(user_id, category, key)
        return {"ok": ok}

    # ── Напоминания ───────────────────────────────────────────────────────────

    @app.get("/api/reminders")
    async def reminders(token: str = Query(default="")):
        _check_token(token)
        user_id = _get_user_id()
        items = get_active_reminders(user_id)
        return {"reminders": [
            {"id": r[0], "text": r[1], "remind_at": r[2]}
            for r in items
        ]}

    @app.post("/api/reminders")
    async def add_reminder(req: ReminderRequest, token: str = Query(default="")):
        _check_token(token)
        try:
            user_id = _get_user_id()
            remind_at = datetime.strptime(req.remind_at, "%Y-%m-%d %H:%M:%S")
            rid = save_reminder(user_id, req.text, remind_at)
            return {"id": rid, "ok": True}
        except HTTPException:
            raise
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @app.delete("/api/reminders/{reminder_id}")
    async def remove_reminder(reminder_id: int, token: str = Query(default="")):
        _check_token(token)
        user_id = _get_user_id()
        ok = delete_reminder(reminder_id, user_id)
        return {"ok": ok}

    # ── Контекст разговора ───────────────────────────────────────────────────

    @app.get("/api/context")
    async def conversation_context(token: str = Query(default="")):
        """Текущий контекст разговора: тема, цель, незавершённые темы, резюме."""
        _check_token(token)
        user_id = _get_user_id()
        return get_conversation_context(user_id)

    @app.delete("/api/context")
    async def clear_context(token: str = Query(default="")):
        """Сбрасывает контекст разговора."""
        _check_token(token)
        user_id = _get_user_id()
        save_conversation_context(user_id)
        return {"ok": True}

    # ── Дневник ───────────────────────────────────────────────────────────────

    @app.get("/api/diary")
    async def diary(limit: int = 20, token: str = Query(default="")):
        _check_token(token)
        user_id = _get_user_id()
        entries = load_diary_entries(user_id, limit=limit)
        return {"entries": [
            {"created_at": e[0], "entry": e[1]}
            for e in entries
        ]}

    # ── Голос ────────────────────────────────────────────────────────────────

    @app.post("/api/voice")
    async def voice_chat(request: Request, token: str = Query(default="")):
        """Принимает аудио (webm/ogg) → Whisper → LLM → TTS."""
        _check_token(token)
        try:
            body = await request.body()
            if not body:
                raise HTTPException(status_code=400, detail="Пустое аудио")

            text = await _voice.transcribe(body)
            if not text:
                return {"text": "", "reply": "Не удалось распознать речь", "audio_base64": None, "agent_name": "raya"}

            user_id = _get_user_id()
            result  = await llm_service.chat(user_id, text, is_voice=True)

            audio_bytes = await _tts.synthesize(result.reply, is_voice=True) if _tts.enabled else None
            audio_b64   = base64.b64encode(audio_bytes).decode() if audio_bytes else None

            return {
                "text":         text,
                "reply":        result.reply,
                "audio_base64": audio_b64,
                "agent_name":   result.agent_name,
                "emotion":      (result.metadata or {}).get("emotion", "calm"),
            }
        except HTTPException:
            raise
        except Exception as e:
            logger.exception("Ошибка голосового чата")
            raise HTTPException(status_code=500, detail=str(e))

    @app.get("/api/tts_enabled")
    async def tts_status(token: str = Query(default="")):
        _check_token(token)
        return {"enabled": _tts.enabled}

    # ── Статус системы ────────────────────────────────────────────────────────

    @app.get("/api/status")
    async def status(token: str = Query(default="")):
        _check_token(token)
        from app.agents.registry import get_enabled_agents
        return {
            "model":    settings.model_name,
            "search":   settings.search_enabled,
            "agents":   [a.name for a in get_enabled_agents()],
            "utc_time": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
        }





    @app.get("/api/features")
    async def features(token: str = Query(default="")):
        """Статус feature flags."""
        _check_token(token)
        from app.feature_flags import status as ff_status
        return ff_status()

    # ── Задачи (SQLite) ───────────────────────────────────────────────────────

    @app.get("/api/tasks")
    async def get_tasks(token: str = Query(default="")):
        """Все активные задачи по квадрантам."""
        _check_token(token)
        user_id = _get_user_id()
        from app.database import get_active_tasks
        tasks = get_active_tasks(user_id)
        _PRIO = {1: "q1", 2: "q2", 3: "q3"}
        result = {
            "q1": {"title": "Срочно и важно",    "emoji": "🔴", "tasks": []},
            "q2": {"title": "Важно, не срочно",  "emoji": "🟡", "tasks": []},
            "q3": {"title": "Срочно, не важно",  "emoji": "🟠", "tasks": []},
        }
        for tid, text, prio, due in tasks:
            q = _PRIO.get(prio, "q3")
            result[q]["tasks"].append({"id": tid, "text": text, "due_date": due, "done": False})
        return result

    @app.post("/api/tasks")
    async def create_task(body: dict, token: str = Query(default="")):
        """Создать задачу."""
        _check_token(token)
        user_id = _get_user_id()
        text = body.get("text", "").strip()
        if not text:
            raise HTTPException(status_code=400, detail="text обязателен")
        from app.database import save_task
        _Q_PRIO = {"q1": 1, "q2": 2, "q3": 3}
        priority = _Q_PRIO.get(body.get("quadrant", "q2"), 2)
        tid = save_task(user_id, text, priority, body.get("due_date", ""))
        return {"id": tid, "ok": True}

    @app.post("/api/tasks/done")
    async def task_done(body: dict, token: str = Query(default="")):
        """Отметить задачу выполненной (по id или тексту)."""
        _check_token(token)
        user_id = _get_user_id()
        from app.database import get_active_tasks, mark_task_done
        task_id = body.get("id")
        text    = body.get("text", "").strip()
        if task_id:
            ok = mark_task_done(int(task_id), user_id)
        elif text:
            tasks = get_active_tasks(user_id)
            ok = False
            for t in tasks:
                if text.lower() in t[1].lower():
                    ok = mark_task_done(t[0], user_id)
                    break
        else:
            raise HTTPException(status_code=400, detail="id или text обязателен")
        return {"ok": ok}

    @app.delete("/api/tasks/{task_id}")
    async def delete_task_route(task_id: int, token: str = Query(default="")):
        """Удалить задачу."""
        _check_token(token)
        user_id = _get_user_id()
        from app.database import delete_task
        ok = delete_task(task_id, user_id)
        return {"ok": ok}

    @app.put("/api/tasks/{task_id}")
    async def update_task_route(task_id: int, body: dict, token: str = Query(default="")):
        """Обновить задачу (текст, приоритет, дедлайн)."""
        _check_token(token)
        user_id = _get_user_id()
        from app.database import get_active_tasks, delete_task, save_task
        tasks = get_active_tasks(user_id)
        task = next((t for t in tasks if t[0] == task_id), None)
        if not task:
            raise HTTPException(status_code=404, detail="Задача не найдена")
        _Q_PRIO = {"q1": 1, "q2": 2, "q3": 3}
        new_text = body.get("text", task[1])
        new_prio = _Q_PRIO.get(body.get("quadrant"), task[2])
        new_due  = body.get("due_date", task[3])
        delete_task(task_id, user_id)
        new_id = save_task(user_id, new_text, new_prio, new_due)
        return {"id": new_id, "ok": True}

    # ── Поиск по истории ──────────────────────────────────────────────────────

    @app.get("/api/search")
    async def search_history(q: str = "", token: str = Query(default="")):
        """Поиск по истории разговора."""
        _check_token(token)
        if not q:
            return {"results": []}
        user_id = _get_user_id()
        from app.database import load_history
        history = load_history(user_id, limit=100)
        q_lower = q.lower()
        results = []
        for msg in history:
            if q_lower in msg.content.lower():
                results.append({
                    "role": "human" if msg.__class__.__name__ == "HumanMessage" else "ai",
                    "snippet": msg.content[:300],
                })
        return {"results": results[:20]}

    # ── Календарь ─────────────────────────────────────────────────────────────

    @app.get("/api/calendar/month")
    async def calendar_month(year: int = 0, month: int = 0,
                             token: str = Query(default="")):
        _check_token(token)
        from datetime import date
        from app.database import get_events_for_month
        today   = date.today()
        y       = year  or today.year
        m       = month or today.month
        user_id = _get_user_id()
        events  = get_events_for_month(user_id, y, m)
        days    = list({e["date"] for e in events})
        return {"events": events, "days_with_events": days}

    @app.get("/api/calendar/day")
    async def calendar_day(date: str = "", token: str = Query(default="")):
        _check_token(token)
        from datetime import date as _date
        from app.database import get_events_for_date
        d       = date or str(_date.today())
        user_id = _get_user_id()
        events  = get_events_for_date(user_id, d)
        return {"date": d, "events": events}

    @app.get("/api/calendar/upcoming")
    async def calendar_upcoming(limit: int = 7, token: str = Query(default="")):
        """Ближайшие события."""
        _check_token(token)
        from app.database import get_upcoming_events
        user_id = _get_user_id()
        return {"events": get_upcoming_events(user_id, limit)}

    @app.post("/api/calendar/events")
    async def calendar_add(body: dict, token: str = Query(default="")):
        """Создать событие."""
        _check_token(token)
        from app.database import save_event
        user_id = _get_user_id()
        title   = body.get("title", "").strip()
        date    = body.get("date", "").strip()
        if not title or not date:
            raise HTTPException(status_code=400, detail="title и date обязательны")
        event_id = save_event(
            user_id=user_id,
            date=date,
            title=title,
            time_start=body.get("time_start", ""),
            time_end=body.get("time_end", ""),
            description=body.get("description", ""),
            color=body.get("color", "blue"),
        )
        return {"id": event_id, "ok": True}

    @app.put("/api/calendar/events/{event_id}")
    async def calendar_update(event_id: int, body: dict, token: str = Query(default="")):
        """Обновить событие."""
        _check_token(token)
        from app.database import update_event
        user_id = _get_user_id()
        ok = update_event(event_id, user_id, **body)
        return {"ok": ok}

    @app.delete("/api/calendar/events/{event_id}")
    async def calendar_delete(event_id: int, token: str = Query(default="")):
        """Удалить событие."""
        _check_token(token)
        from app.database import delete_event
        user_id = _get_user_id()
        ok = delete_event(event_id, user_id)
        return {"ok": ok}

    @app.post("/api/calendar/day_notes")
    async def calendar_day_notes(body: dict, token: str = Query(default="")):
        """Сохраняет заметки дня в дневник (БД)."""
        _check_token(token)
        date  = body.get("date", "")
        notes = body.get("notes", "").strip()
        if not date:
            raise HTTPException(status_code=400, detail="date обязателен")
        if notes:
            user_id = _get_user_id()
            from app.database import save_diary_entry
            save_diary_entry(user_id, f"[{date}] {notes}")
        return {"ok": True}

    logger.info("Веб-сервер создан")
    return app
