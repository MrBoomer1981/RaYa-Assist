"""
web_server.py — FastAPI веб-интерфейс RaYa.
Защита через токен в URL: /?token=YOUR_TOKEN
Все разделы: чат, память, дневник, напоминания.
"""
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.config import settings
from app.database import (
    clear_history, clear_memory,
    delete_reminder, get_active_reminders,
    load_diary_entries, load_history, load_memory,
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

# ── Приложение ────────────────────────────────────────────────────────────────

def create_app(llm_service) -> FastAPI:
    """
    Фабрика FastAPI приложения.
    llm_service передаётся снаружи — тот же экземпляр что использует бот.
    """
    app = FastAPI(title="RaYa", docs_url=None, redoc_url=None)

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
            user_id = settings.telegram_user_id
            result = await llm_service.chat(user_id, req.message)

            # Изображение от ImageAgent — сохраняем и отдаём URL
            image_url: Optional[str] = None
            if "image" in result.agent_name:
                image_bytes = (result.metadata or {}).get("image_bytes")
                if image_bytes:
                    import uuid
                    fname = f"{uuid.uuid4().hex}.jpg"
                    fpath = _MEDIA_DIR / fname
                    fpath.write_bytes(image_bytes)
                    image_url = f"/static/media/{fname}"
                    logger.info("🎨 Изображение сохранено: %s", fname)

            return {
                "reply":      result.reply,
                "agent_name": result.agent_name,
                "reminder":   result.reminder,
                "image_url":  image_url,
            }
        except Exception as e:
            logger.exception("Ошибка чата")
            raise HTTPException(status_code=500, detail=str(e))

    @app.get("/api/history")
    async def history(token: str = Query(default="")):
        _check_token(token)
        user_id = settings.telegram_user_id
        messages = load_history(user_id, limit=50)
        return [
            {"role": "human" if m.__class__.__name__ == "HumanMessage" else "ai",
             "content": m.content}
            for m in messages
        ]

    @app.delete("/api/history")
    async def delete_history(token: str = Query(default="")):
        _check_token(token)
        user_id = settings.telegram_user_id
        clear_history(user_id)
        return {"ok": True}

    # ── Память ────────────────────────────────────────────────────────────────

    @app.get("/api/memory")
    async def memory(token: str = Query(default="")):
        _check_token(token)
        user_id = settings.telegram_user_id
        facts = load_memory(user_id)
        return {"facts": facts}

    @app.delete("/api/memory")
    async def delete_memory(token: str = Query(default="")):
        _check_token(token)
        user_id = settings.telegram_user_id
        clear_memory(user_id)
        return {"ok": True}

    # ── Напоминания ───────────────────────────────────────────────────────────

    @app.get("/api/reminders")
    async def reminders(token: str = Query(default="")):
        _check_token(token)
        user_id = settings.telegram_user_id
        items = get_active_reminders(user_id)
        return {"reminders": [
            {"id": r[0], "text": r[1], "remind_at": r[2]}
            for r in items
        ]}

    @app.post("/api/reminders")
    async def add_reminder(req: ReminderRequest, token: str = Query(default="")):
        _check_token(token)
        try:
            user_id = settings.telegram_user_id
            remind_at = datetime.strptime(req.remind_at, "%Y-%m-%d %H:%M:%S")
            rid = save_reminder(user_id, req.text, remind_at)
            return {"id": rid, "ok": True}
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @app.delete("/api/reminders/{reminder_id}")
    async def remove_reminder(
        reminder_id: int,
        token: str = Query(default=""),
    ):
        _check_token(token)
        user_id = settings.telegram_user_id
        ok = delete_reminder(reminder_id, user_id)
        return {"ok": ok}

    # ── Дневник ───────────────────────────────────────────────────────────────

    @app.get("/api/diary")
    async def diary(limit: int = 20, token: str = Query(default="")):
        _check_token(token)
        user_id = settings.telegram_user_id
        entries = load_diary_entries(user_id, limit=limit)
        return {"entries": [
            {"created_at": e[0], "entry": e[1]}
            for e in entries
        ]}

    # ── Статус системы ────────────────────────────────────────────────────────

    @app.get("/api/status")
    async def status(token: str = Query(default="")):
        _check_token(token)
        from app.agents.registry import get_enabled_agents
        from app.config import settings
        return {
            "model":        settings.model_name,
            "search":       settings.search_enabled,
            "agents":       [a.name for a in get_enabled_agents()],
            "utc_time":     datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
        }

    logger.info("🌐 Веб-сервер создан")
    return app
